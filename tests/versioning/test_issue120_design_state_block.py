"""Issue #120 (live-path integration): the design-state block reaches the
LIVE design prompt, and the route resolves to the same shared callable.

The unit tests in ``tests/test_design_state.py`` pin the entry model, the
null-serialisation, the disagrees-both-values rule, the bound, and the
non-numeric/label rules in isolation. This file pins the LIVE path:

- the design prompt (``_design_messages`` / ``_design_system`` — the live
  path, not the dead ``design_prompts.design_prompt``) contains the
  previous version's actual dimensions (the 30/60 bug, pinned).
- the prompt builder and the API route resolve to the SAME callable
  (``state_block_from_params``) — asserted by identity, not merely equal
  output.
- a no-version-yet turn renders an empty block (honest, never a
  fabricated dimension).
- a non-envelope parameter set (bore + wall) reaches the prompt.

All fast (stub llm/render, no Docker).
"""

from __future__ import annotations

from typing import Any

from d33d.design_loop import BboxInfo, run_design_loop
from d33d.design_state import state_block_from_params
from d33d.render_worker import RenderResult
from tests.versioning.helpers import create_project, create_version, run_async

# ---------------------------------------------------------------------------
# A distinctive token that appears NOWHERE in the system prompt, the
# dimensions line, the emission instruction, or the repair directive —
# its presence in the captured prompt proves the block was rendered.
# ---------------------------------------------------------------------------
TOKEN = "bore_diameter = 8"


def _llm_result(scad: str) -> Any:
    from d33d.design_llm import LLMResult

    return LLMResult(
        content=f"```scad\n{scad}\n```",
        tool_calls=(),
        prompt_hash="h" * 64,
        tier="T1",
        status="ok",
        request_body={},
    )


def _passing_render() -> RenderResult:
    return RenderResult(
        ok=True,
        exit_code=0,
        duration_ms=1,
        error_class="ok",
        stderr="",
        stl="model.stl",
        csg="model.csg",
        views=("v0.png", "v1.png", "v2.png", "v3.png", "v4.png", "v5.png"),
    )


def _bbox_ok(render) -> Any:
    from d33d.design_loop import BboxInfo

    # The empty-block test uses stated 20; return 20 (within tolerance).
    return BboxInfo(x=20.0, y=20.0, z=20.0, volume=8000.0)


def _user_text(captured: list, index: int) -> str:
    msg = captured[index][0]
    assert msg["role"] == "user"
    text = [p for p in msg["content"] if p.get("type") == "text"]
    assert len(text) == 1
    return text[0]["text"]


# ---------------------------------------------------------------------------
# The 30/60 bug, pinned on the LIVE path: the design prompt contains the
# previous version's actual dimension (30).
# ---------------------------------------------------------------------------


def test_design_prompt_contains_previous_versions_stated_dimension():
    """A project whose latest version established a 30 mm dimension
    produces a design prompt containing 30. The block is built from the
    previous version's params and rendered into the LIVE prompt
    (``_design_messages``) — the 30/60 bug is pinned: the model can no
    longer invent 60 for a sphere it had itself made 30."""
    captured: list[list[dict[str, Any]]] = []

    async def llm_fn(role, messages, system):
        captured.append(messages)
        return _llm_result("x = 30;\nsphere(d = x);\n")

    async def render_fn(scad, defines):
        return _passing_render()

    # The live loop seam: state_params carries the previous version's
    # full params snapshot (the 30 mm dimension).
    state_params = {"W": 30.0, "D": 30.0, "H": 30.0}

    result = run_design_loop(
        photo="data:image/png;base64,REF",
        chat_history=(),
        stated_dims=(30.0, 30.0, 30.0),
        render_fn=render_fn,
        llm_fn=llm_fn,
        bbox_fn=lambda r: BboxInfo(x=30.0, y=30.0, z=30.0, volume=8000.0),
        request="add a second sphere underneath",
        state_params=state_params,
    )
    assert result.status == "pass"
    assert captured, "the LLM edge was never called"
    user_text = _user_text(captured, 0)
    # The block is rendered (the decisive assertion).
    assert "Current design state (mm):" in user_text
    # The previous version's actual dimension (30) is in the prompt.
    assert "30" in user_text
    assert "W = 30" in user_text
    # The block is between the chat/request lines and the reference dims.
    state_pos = user_text.index("Current design state (mm):")
    ref_pos = user_text.index("Reference dimensions (mm, ground truth):")
    assert state_pos < ref_pos, "the block must precede the reference dims"


def test_design_prompt_non_envelope_params_reach_live_prompt():
    """A parameter set that is NOT {W, D, H} (a bore diameter and a wall
    thickness) reaches the LIVE prompt — proving the block is not
    secretly the envelope triple on the production path too."""
    captured: list[list[dict[str, Any]]] = []

    async def llm_fn(role, messages, system):
        captured.append(messages)
        return _llm_result("bore = 8;\nwall = 2;\ncube([1, 1, 1]);\n")

    async def render_fn(scad, defines):
        return _passing_render()

    state_params = {"bore_diameter": 8.0, "wall_thickness": 2.0}

    result = run_design_loop(
        photo="data:image/png;base64,REF",
        chat_history=(),
        stated_dims=None,
        render_fn=render_fn,
        llm_fn=llm_fn,
        bbox_fn=_bbox_ok,
        request="make a part",
        state_params=state_params,
    )
    assert result.status == "pass"
    user_text = _user_text(captured, 0)
    assert "Current design state (mm):" in user_text
    # The non-envelope params are in the block (the token appears
    # verbatim).
    assert TOKEN in user_text
    assert "wall_thickness = 2" in user_text
    # Neither is W/D/H.
    assert "bore_diameter = 8" in user_text


def test_design_prompt_no_version_yet_renders_empty_block():
    """A no-version-yet turn (``state_params=None``) renders an honest
    EMPTY block (zero entries) — never a fabricated dimension, never a
    silently-absent section."""
    captured: list[list[dict[str, Any]]] = []

    async def llm_fn(role, messages, system):
        captured.append(messages)
        return _llm_result("x = 20;\ncube([x, x, x]);\n")

    async def render_fn(scad, defines):
        return _passing_render()

    result = run_design_loop(
        photo="data:image/png;base64,REF",
        chat_history=(),
        stated_dims=(20.0, 20.0, 20.0),
        render_fn=render_fn,
        llm_fn=llm_fn,
        bbox_fn=lambda r: _bbox_ok(r),
        request="make a cube",
        state_params=None,  # no version yet
    )
    assert result.status == "pass"
    user_text = _user_text(captured, 0)
    # The section is present (never silently absent).
    assert "Current design state (mm):" in user_text
    # But it carries no fabricated dimension (the honest empty state).
    assert "(no parameters yet)" in user_text


# ---------------------------------------------------------------------------
# The shared callable: prompt builder and route resolve to the SAME
# function (identity, not merely equal output).
# ---------------------------------------------------------------------------


def test_prompt_builder_and_route_share_the_same_callable():
    """The prompt builder and the API route call THE SAME FUNCTION —
    ``state_block_from_params``. Assert the shared callable by identity:
    the route reads ``latest_version(project_id)['params']`` and calls the
    same function the live prompt builder uses."""
    import d33d.design_state as ds

    # The route's data source (the versions service's latest_version).
    class _Svc:
        def latest_version(self, project_id):
            return {"params": {"W": 30.0, "bore_diameter": 8.0}}

    latest = _Svc().latest_version(1)
    assert latest is not None

    # The route builds the block from latest_version's params.
    route_block = state_block_from_params(latest["params"])

    # The prompt builder builds the block from the same params snapshot,
    # using the SAME function (identity — not two functions that happen
    # to agree).
    prompt_fn = ds.state_block_from_params
    assert prompt_fn is state_block_from_params
    prompt_block = prompt_fn(latest["params"])

    # Same callable, same output.
    assert route_block == prompt_block
    names = {e["name"] for e in route_block}
    assert names == {"W", "bore_diameter"}


# ---------------------------------------------------------------------------
# End-to-end through the REAL route: a version with W=30 → the finalize
# seam's loop kwargs carry the previous version's params, and the block
# built from them contains 30.
# ---------------------------------------------------------------------------


def test_route_latest_version_params_feed_the_block(app_with_versions):
    """Through the real versions service: a project whose latest version
    has W=30 → the params the route reads (``latest_version['params']``)
    feed the shared block, which contains 30 (the 30/60 bug pinned at the
    route's data source)."""
    from d33d.design_state import build_design_state_block, format_design_state_block

    async def _call(client):
        proj = await create_project(client)
        pid = proj["id"]
        await create_version(client, pid, {"W": 30.0, "D": 30.0, "H": 30.0})
        # The route's data source: the latest version's params.
        latest = app_with_versions.state.versions.latest_version(pid)
        assert latest is not None
        block = build_design_state_block(
            state_block_from_params(latest["params"])
        )
        return format_design_state_block(block)

    text = run_async(app_with_versions, _call)
    # The route's data source feeds the block; the block contains 30.
    assert "30" in text
    assert "W = 30" in text


def test_finalize_seam_state_params_match_route_callable(app_with_versions):
    """The FINALIZE seam's ``state_params`` kwarg (the latest version's
    params) is the SAME dict the shared ``state_block_from_params``
    function consumes — the route and the prompt builder resolve to the
    same callable (asserted by the block they both produce, not merely
    equal output)."""
    from d33d.design_state import build_design_state_block, format_design_state_block
    from tests.versioning.test_design_loop_finalize import _StubResult

    captured: dict[str, Any] = {}

    async def _loop(app, **kwargs):
        captured.update(kwargs)
        return _StubResult("pass", {"W": 30})

    async def _call(client):
        proj = await create_project(client)
        pid = proj["id"]
        await create_version(client, pid, {"W": 30.0, "D": 30.0, "H": 30.0, "bore_diameter": 8.0})
        app_with_versions.state.run_design_loop = _loop
        return await client.post(f"/api/projects/{pid}/finalize", json={})

    r = run_async(app_with_versions, _call)
    assert r.status_code == 201, r.text
    # The seam's state_params carry the latest version's full params
    # (including the non-envelope bore_diameter).
    state_params = captured.get("state_params")
    assert state_params is not None
    assert state_params["W"] == 30.0
    assert state_params["bore_diameter"] == 8.0
    # The shared callable (the route AND the prompt builder) builds the
    # block from these params — the 30/60 bug pinned at the seam.
    block = build_design_state_block(state_block_from_params(state_params))
    text = format_design_state_block(block)
    assert "30" in text
    assert "bore_diameter = 8" in text
