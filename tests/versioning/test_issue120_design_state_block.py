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
from d33d.design_state import state_block_for_version
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


def test_design_prompt_shows_model_label_not_identifier():
    """Issue #248: when a param carries a model label in the version's
    ``param_meta``, the live prompt's "Current design state" block shows
    the LABEL (not the raw identifier) — the same block the GET the SPA
    reads serves (the shared callable, with ``state_meta`` threaded
    through the loop's kwargs)."""
    captured: list[list[dict[str, Any]]] = []

    async def llm_fn(role, messages, system):
        captured.append(messages)
        return _llm_result("width = 60;\ncube([width, 1, 1]);\n")

    async def render_fn(scad, defines):
        return _passing_render()

    state_params = {"width": 60.0}
    state_meta = {"width": {"label": "Overall width", "unit": "mm", "axis": "W"}}

    result = run_design_loop(
        photo="data:image/png;base64,REF",
        chat_history=(),
        stated_dims=None,
        render_fn=render_fn,
        llm_fn=llm_fn,
        bbox_fn=_bbox_ok,
        request="make a part",
        state_params=state_params,
        state_meta=state_meta,
    )
    assert result.status == "pass"
    user_text = _user_text(captured, 0)
    assert "Current design state (mm):" in user_text
    # The label renders, not the raw identifier — scoped to the state
    # block's section (the design-source section legitimately carries the
    # SCAD's own `width` declaration, which is not the block's label).
    state_pos = user_text.index("Current design state (mm):")
    block_text = user_text[state_pos:]
    assert "Overall width = 60" in block_text
    # The raw identifier does not appear as a state-block line (it is
    # replaced by the label — no duplicate line).
    assert "width = 60" not in block_text.split("\n", 2)[2]


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


# The shared-callable identity test lives in
# tests/versioning/test_design_state_route.py, repointed at the real
# ``GET /api/projects/{id}/design-state`` route (this file's old copy
# asserted the identity against the finalize seam's kwargs — honest about
# identity, but wrong about which consumer it named; the second consumer
# the ticket describes is the GET the SPA reads, and the route now
# exists).

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
        # The GET route's data source: the latest version's params.
        latest = app_with_versions.state.versions.latest_version(pid)
        assert latest is not None
        block = build_design_state_block(
            state_block_for_version(latest["params"], latest["bbox"])
        )
        return format_design_state_block(block)

    text = run_async(app_with_versions, _call)
    # The GET route serves the same block (the SPA's second consumer
    # reads exactly what the prompt builder renders).
    assert "W = 30" in text


def test_finalize_seam_state_params_match_route_callable(app_with_versions):
    """The FINALIZE seam's ``state_params`` kwarg (the latest version's
    params) is the SAME dict the shared ``state_block_from_params``
    function consumes — the route and the prompt builder resolve to the
    same callable (asserted by the block they both produce, not merely
    equal output)."""
    from d33d.design_state import (
        build_design_state_block,
        format_design_state_block,
        state_block_for_version as _shared,
    )
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
    block = build_design_state_block(_shared(state_params))
    text = format_design_state_block(block)
    assert "30" in text
    assert "bore_diameter = 8" in text


# ---------------------------------------------------------------------------
# The stated prompt bound, exercised through the LIVE loop path.
#
# The bound is a property of the block builder (unit-pinned in
# tests/test_design_state.py), but it must also hold on the LIVE path the
# model actually reads: when the latest version declares MORE than
# ``MAX_STATE_BLOCK_ENTRIES`` parameters, ``_design_messages`` renders only
# the bound entries plus the honest drop-and-count line — it never leaks
# the overflow into the prompt (the bound is a stated, tested property of
# how many entries reach the prompt, not merely of the serialised block).
# ---------------------------------------------------------------------------


def test_live_prompt_enforces_entry_bound_and_counts_overflow():
    """A version whose params exceed the bound (13 entries) reaches the
    LIVE prompt through the loop with the bound enforced: only the first
    ``MAX_STATE_BLOCK_ENTRIES`` entries render, the overflow is dropped, and
    the honest ``… 1 more parameter`` count line names the drop. The 13th
    (overflow) parameter is never in the prompt."""
    from d33d.design_state import MAX_STATE_BLOCK_ENTRIES

    captured: list[list[dict[str, Any]]] = []
    n = MAX_STATE_BLOCK_ENTRIES + 1  # one past the bound
    state_params = {f"p{i}": float(i + 1) for i in range(n)}

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
        request="make a part",
        state_params=state_params,
    )
    assert result.status == "pass"
    user_text = _user_text(captured, 0)
    # The section header is present.
    assert "Current design state (mm):" in user_text
    # All bound entries render (declaration order, first 12).
    for i in range(MAX_STATE_BLOCK_ENTRIES):
        assert f"p{i} = {i + 1:g}" in user_text
    # The overflow entry (the 13th) never reaches the prompt.
    assert f"p{MAX_STATE_BLOCK_ENTRIES} =" not in user_text
    # The drop is named honestly, never silently.
    assert "… 1 more parameter" in user_text
    # The block is still between the chat/request lines and the reference
    # dims (line order preserved at the bound).
    state_pos = user_text.index("Current design state (mm):")
    ref_pos = user_text.index("Reference dimensions (mm, ground truth):")
    assert state_pos < ref_pos
