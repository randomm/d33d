"""Issue #97 — the user's current request must reach the RENDERED prompt.

The defect: ``_design_messages`` built the user text from ``chat_history``
alone, and the SPA's chat_history excludes the current turn, so the model
never saw what was asked (it invented unrelated geometry). The fix threads
the current message as an explicit ``request`` parameter through
``run_design_loop`` / ``run_design_loop_async`` and renders it as the
FIRST line of the user text, labelled ``Request:`` — before the
prior-context ``chat:`` lines, the dimensions line, and the emission
instruction.

The decisive tests below assert on the RENDERED prompt string captured by
a fake ``llm_fn`` — never on a parameter being passed (the entire defect
is that a value was passed around six hops and never rendered).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from d33d.design_loop import run_design_loop
from d33d.render_worker import RenderResult

# A distinctive token that appears NOWHERE in the system prompt, the
# dimensions line, the emission instruction, or the repair directive —
# its presence in the captured prompt proves the request was rendered.
TOKEN = "a 12mm sphere beside the cube"


def _llm_result(tool_calls: tuple[dict[str, Any], ...]) -> Any:
    from d33d.design_llm import LLMResult

    return LLMResult(
        content="",
        tool_calls=tool_calls,
        prompt_hash="h" * 64,
        tier="T1",
        status="ok",
        request_body={},
    )


def _passing_render() -> RenderResult:
    """A clean render whose views/bbox all pass against any stated dims
    (bbox_fn=None is never reached — the render is already ok)."""
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


def _bbox_ok_all(render: RenderResult):
    """A bbox_fn that passes every axis for any stated triple."""
    from d33d.design_loop import BboxInfo

    return BboxInfo(x=2.0, y=2.0, z=2.0, volume=1.0)


def _run(
    *,
    scad_script: Sequence[str],
    request: str,
    chat_history: Sequence[str] = (),
    stated: tuple[float, float, float] | None = None,
    bbox=None,
    max_iterations: int = 3,
):
    """Drive the REAL loop with a fake llm_fn that captures every prompt."""
    captured: list[list[dict[str, Any]]] = []
    i = {"n": 0}

    async def llm_fn(role, messages, system):
        captured.append(messages)
        scad = scad_script[min(i["n"], len(scad_script) - 1)]
        return _llm_result(({"tool": "emit_design", "arguments": {"scad": scad}},))

    async def render_fn(scad, defines):
        i["n"] += 1
        return _passing_render()

    result = run_design_loop(
        photo="data:image/png;base64,REF",
        stated_dims=stated,
        render_fn=render_fn,
        llm_fn=llm_fn,
        bbox_fn=bbox,
        request=request,
        chat_history=chat_history,
        max_iterations=max_iterations,
    )
    return result, captured


def _user_text(captured: list, index: int) -> str:
    """The rendered text part of the captured user message at ``index``."""
    msg = captured[index][0]
    assert msg["role"] == "user"
    parts = msg["content"]
    text = [p for p in parts if p.get("type") == "text"]
    assert len(text) == 1
    return text[0]["text"]


# ---------------------------------------------------------------------------
# The decisive test: request rendered into the prompt, EXACT SPA shape
# (message + EMPTY chat_history — a non-empty history would mask the bug)
# ---------------------------------------------------------------------------


def test_request_renders_into_prompt_on_fresh_project_empty_history():
    """First turn, ``chat_history=[]`` (the exact SPA shape that produced
    the bug): the distinctive token must appear in the RENDERED prompt
    text, as a ``Request:`` line BEFORE the dimensions line and the
    emission instruction. The system prompt is unchanged (no token there).
    """
    scad = "x = 12;\nsphere(d = x);\n"
    result, captured = _run(
        scad_script=[scad],
        request=f"Please create {TOKEN}",
        chat_history=(),
        stated=None,
        bbox=_bbox_ok_all,
    )
    assert result.status == "pass"
    assert len(captured) == 1
    user_text = _user_text(captured, 0)
    # The token is in the RENDERED user text (the decisive assertion).
    assert TOKEN in user_text, f"request token missing from rendered prompt:\n{user_text}"
    # Labelled, FIRST line, before the dimensions line and emission
    # instruction (the specified order).
    assert user_text.startswith("Request: Please create " + TOKEN)
    dim_pos = user_text.index("Reference dimensions (mm, ground truth):")
    emit_pos = user_text.index("Emit parametric OpenSCAD.")
    req_pos = 0  # the request line is the first line
    assert req_pos < dim_pos < emit_pos
    # The system prompt is UNCHANGED by the fix (no request there).
    # (The loop's design system prompt is built by _design_system — the
    # token is nowhere in it; the decisive surface is the user text.)


def test_design_prompt_instructs_title_comment() -> None:
    """Issue #245: the live design-role emission instruction asks the model
    to start the SCAD with a ``// title:`` comment (the primary version-name
    source). Asserted on the REAL loop's rendered prompt (the production
    path — ``_design_messages``), not the dead ``design_prompt``."""
    scad = "x = 12;\nsphere(d = x);\n"
    result, captured = _run(
        scad_script=[scad],
        request="make a thing",
        chat_history=(),
        stated=None,
        bbox=_bbox_ok_all,
    )
    assert result.status == "pass"
    user_text = _user_text(captured, 0)
    assert "// title:" in user_text, (
        f"title instruction missing from the design emission prompt:\n{user_text}"
    )


def test_request_renders_before_chat_history_and_dims_on_followup_turn():
    """Follow-up turn: the request appears AND prior chat_history turns
    still appear — in the specified order: Request first, then the
    ``chat:`` prior-context lines, then the dimensions line, then the
    emission instruction."""
    scad = "x = 12;\nsphere(d = x);\n"
    result, captured = _run(
        scad_script=[scad],
        request=f"now make {TOKEN} instead",
        chat_history=("make a cube please", "and add a hole"),
        stated=None,
        bbox=_bbox_ok_all,
    )
    assert result.status == "pass"
    user_text = _user_text(captured, 0)
    req_pos = user_text.index("Request: now make " + TOKEN + " instead")
    assert req_pos == 0, "the Request line must be the first line"
    chat1 = user_text.index("chat: make a cube please")
    chat2 = user_text.index("chat: and add a hole")
    dim_pos = user_text.index("Reference dimensions (mm, ground truth):")
    emit_pos = user_text.index("Emit parametric OpenSCAD.")
    assert req_pos < chat1 < chat2 < dim_pos < emit_pos


# ---------------------------------------------------------------------------
# The repair iteration (iteration 2) must ALSO carry the request
# ---------------------------------------------------------------------------


def test_repair_iteration_prompt_contains_request():
    """Iteration 2 (the repair pass) renders the request verbatim in its
    user text too — the repair block alone (\"does not match the reference
    photo or the stated dimensions\") is not enough to steer toward what
    was asked."""
    # Iteration 1: valid SCAD but magic-number geometry (the named-params
    # gate fails) + a blank view -> the loop routes a repair to iteration 2.
    bad_scad = "cube([20, 25, 30]);\n"
    good_scad = "W = 20;\nD = 25;\nH = 30;\ncube([W, D, H]);\n"
    scad_script = [bad_scad, good_scad, good_scad]

    captured: list[list[dict[str, Any]]] = []
    i = {"n": 0}
    blanks: dict[int, bool] = {1: True}  # iteration 1 -> one blank view

    async def llm_fn(role, messages, system):
        captured.append(messages)
        scad = scad_script[min(i["n"], len(scad_script) - 1)]
        return _llm_result(({"tool": "emit_design", "arguments": {"scad": scad}},))

    async def render_fn(scad, defines):
        n = i["n"] + 1
        i["n"] += 1
        views = (
            ("v0.png", "v1.png", "v2.png", "v3.png", "v4.png", "v5.png")
            if not blanks.get(n)
            else ("v0.png", "v1.png", "v2.png", "v3.png", "v4.png", "")
        )
        return RenderResult(
            ok=True,
            exit_code=0,
            duration_ms=1,
            error_class="ok",
            stderr="",
            stl="model.stl",
            csg="model.csg",
            views=views,
        )

    _result = run_design_loop(
        photo="data:image/png;base64,REF",
        stated_dims=(20.0, 25.0, 30.0),
        render_fn=render_fn,
        llm_fn=llm_fn,
        bbox_fn=_bbox_ok_all,
        request=f"Please create {TOKEN}",
        chat_history=(),
        max_iterations=3,
    )
    del _result
    # At least two iterations ran (the repair path fired).
    assert len(captured) >= 2, "expected a repair iteration, got one call"
    # Iteration 1's prompt carries the request (the baseline).
    assert TOKEN in _user_text(captured, 0)
    # Iteration 2's (the repair) prompt ALSO carries it.
    assert TOKEN in _user_text(captured, 1), (
        "repair-iteration prompt must contain the request:\n"
        + _user_text(captured, 1)
    )
    # The repair block is present on iteration 2 (the directive fired).
    assert "REPAIR directive" in _user_text(captured, 1)
    # The request precedes the repair block (it is the first line).
    assert _user_text(captured, 1).startswith("Request: Please create " + TOKEN)


# ---------------------------------------------------------------------------
# End-to-end through the production closure: the hook must NOT swallow
# the request (failure_capture._hooked used to POP it for archiving and
# drop it — the value must now reach run_design_loop_async too).
# ---------------------------------------------------------------------------


def test_production_closure_forwards_request_past_the_hook(app_with_versions, monkeypatch):
    """The REAL production closure (``_build_production_design_loop``) +
    the REAL hook (``default_run_design_loop_hook``): a ``request``
    threaded through ``**kwargs`` must SURVIVE the hook's pop and reach
    ``run_design_loop_async`` (the hook archives a copy AND forwards it).
    The loop is stubbed to capture the kwargs it is actually handed; the
    catalogue/probe/llm deps are stubbed (no live LLM/Docker).
    """
    import types

    from d33d import design_loop as _dl
    from d33d.app import _build_production_design_loop
    from d33d.config import catalogue as _cat
    from d33d.config import probes as _probes
    from d33d.config import resolve as _res

    captured: dict[str, Any] = {}

    class _Result:
        status = "pass"
        failure_reason = None

        class _Best:
            scad_source = "x = 20; cube([x, x, x]);"

        best = _Best()

    async def _fake_real_run(**kwargs):
        captured.update(kwargs)
        return _Result()

    async def _fake_probe(base_url, model_id, api_key, request_factory):
        return None

    async def _noop_llm(*a, **k):
        return ""

    monkeypatch.setattr(
        _cat,
        "load_catalogue",
        lambda p: types.SimpleNamespace(
            providers={"p": types.SimpleNamespace(key="stub")}
        ),
    )
    monkeypatch.setattr(
        _res,
        "resolve_model",
        lambda cat, role: types.SimpleNamespace(
            entry=types.SimpleNamespace(model="stub"),
            provider=types.SimpleNamespace(base="http://stub"),
        ),
    )
    monkeypatch.setattr(_probes, "probe_capabilities", _fake_probe)
    monkeypatch.setattr(_dl, "make_llm_fn", lambda cat, f, c: _noop_llm)
    monkeypatch.setattr(_dl, "run_design_loop_async", _fake_real_run)

    async def _call(client):
        app_with_versions.state.failures_jsonl_path = str(
            app_with_versions.state.catalogue_path.parent / "failures.jsonl"
        )
        closure = _build_production_design_loop()
        return await closure(
            app=app_with_versions,
            photo="data:image/png;base64,x",
            chat_history=("prior turn",),
            stated_dims=(1.0, 2.0, 3.0),
            render_fn=None,
            llm_fn=None,
            request=f"Please create {TOKEN}",
        )

    from tests.versioning.helpers import run_async

    result = run_async(app_with_versions, _call)
    assert result.status == "pass"
    # The CRITICAL trap: the hook used to pop ``request`` and the value
    # never reached the loop. It must now.
    assert "request" in captured, (
        "the hook swallowed the request — it never reached run_design_loop_async"
    )
    assert captured["request"] == f"Please create {TOKEN}"


# ---------------------------------------------------------------------------
# Issue #213 regression: the REAL ``render_fn`` closure built INSIDE
# ``_build_production_design_loop`` must call ``render_for_design_loop``
# with exactly its 4-parameter contract. Commit cbe629e (issue #118) added
# a stray ``on_progress_iteration="_current"`` kwarg to the call site; with
# the real worker signature, every design loop crashed with
# ``TypeError: render_for_design_loop() got an unexpected keyword
# argument 'on_progress_iteration'`` before any render could run. The
# iteration index flows via the ``on_progress._current`` attribute stamp
# (``d33d.design_loop._stamp_on_progress_iteration``), never as a kwarg.
# ---------------------------------------------------------------------------


def test_production_closure_render_fn_matches_worker_signature(
    app_with_versions, monkeypatch
):
    """Drive the REAL ``_build_production_design_loop`` closure (catalogue /
    probe / llm monkeypatched) with the REAL ``run_design_loop_async``
    intact; only ``d33d.app.render_for_design_loop`` is swapped for a
    signature-exact spy. The closure-internal ``render_fn`` is NOT stubbed
    (stubbing it — or the loop — would defeat the regression): the real
    loop calls it and the spy's exact 4-parameter signature raises
    ``TypeError`` at the call site if the closure passes any extra kwarg.
    """
    import types

    from d33d import app as _app
    from d33d.config import catalogue as _cat
    from d33d.config import probes as _probes
    from d33d.config import resolve as _res
    from d33d.render_worker import RenderResult

    calls: list[dict[str, Any]] = []

    # Signature EXACTLY matches ``render_for_design_loop`` — no
    # ``**kwargs`` catch-all: a stray kwarg from the buggy closure is a
    # ``TypeError`` at call time, which is the guard.
    def _spy(
        scad_source: str,
        defines: dict[str, str],
        renders_dir=None,
        on_progress=None,
    ) -> RenderResult:
        calls.append({"renders_dir": renders_dir, "on_progress": on_progress})
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

    monkeypatch.setattr(_app, "render_for_design_loop", _spy)
    monkeypatch.setattr(
        _cat,
        "load_catalogue",
        lambda p: types.SimpleNamespace(
            providers={"p": types.SimpleNamespace(key="stub")}
        ),
    )
    monkeypatch.setattr(
        _res,
        "resolve_model",
        lambda cat, role: types.SimpleNamespace(
            entry=types.SimpleNamespace(model="stub"),
            provider=types.SimpleNamespace(base="http://stub"),
        ),
    )

    async def _fake_probe(base_url, model_id, api_key, request_factory):
        return None

    monkeypatch.setattr(_probes, "probe_capabilities", _fake_probe)

    # ``stated_dims=(20.0, 25.0, 30.0)`` so the loop's defines map carries
    # ``W/D/H`` as the values the emitted SCAD names — the named-params
    # gate (bit 4) passes on iteration 1 and the loop returns a clean
    # result without needing further iterations.
    scad = "W = 20;\nD = 25;\nH = 30;\ncube([W, D, H]);\n"

    # The bbox gate's declared path: the loop receives the production
    # ``bbox_fn`` (``bbox_from_render`` — the issue #54 chat-route wiring).
    # With the spy's ``RenderResult.stl`` pointing at a dead path, every
    # axis is None and ``_bbox_within_tolerance`` abstains (``target <= 0``
    # never fires — the stated dims are real), so the loop exhausts on
    # iteration 3 (``NO_IMPROVEMENT_LIMIT`` consecutive no-improvement
    # steps) instead of burning the 3-iteration cap. That is the intended
    # shape for a kwarg-contract guard: the loop's stopping reason is
    # irrelevant, only the render_fn call contract matters.
    from d33d.design_loop_events import bbox_from_render

    async def _llm_fn(role, messages, system):
        return _llm_result(
            ({"tool": "emit_design", "arguments": {"scad": scad}},)
        )

    from d33d import design_loop as _dl

    monkeypatch.setattr(_dl, "make_llm_fn", lambda cat, f, c: _llm_fn)

    async def _call(client):
        app_with_versions.state.failures_jsonl_path = str(
            app_with_versions.state.catalogue_path.parent / "failures.jsonl"
        )
        closure = _app._build_production_design_loop()
        return await closure(
            app=app_with_versions,
            photo="data:image/png;base64,x",
            chat_history=(),
            stated_dims=(20.0, 25.0, 30.0),
            render_fn=None,
            llm_fn=None,
            bbox_fn=bbox_from_render,
            request=f"Please create {TOKEN}",
        )

    from tests.versioning.helpers import run_async

    result = run_async(app_with_versions, _call)
    # The loop completed through the REAL render_fn into the spy — no
    # ``TypeError`` from an unexpected kwarg (the issue #213 defect). The
    # loop makes up to ``max_iterations`` render calls (it exhausts here
    # because the bbox gate abstains on the spy's dead STL path — that is
    # the intended shape for a kwarg-contract guard; the stopping reason
    # is irrelevant, only the render_fn call contract matters).
    assert len(calls) >= 1, "the loop never reached the render_fn"
    assert all(
        set(call) == {"renders_dir", "on_progress"} for call in calls
    )
    assert result.status in ("pass", "exhausted")


# ---------------------------------------------------------------------------
# failures.jsonl: the archive ``request`` equals the CURRENT request (not
# a prior-turn chat_history fallback) for a fresh-project exhaustion.
# ---------------------------------------------------------------------------


def test_failures_jsonl_request_field_equals_current_request(tmp_path, monkeypatch):
    """After the fix, an exhausted loop's failures.jsonl ``request`` field
    is the CURRENT user's request (the hook archives the same value it
    forwards to the loop) — never the prior-turn ``chat_history``
    fallback the hook used to degrade to."""
    from d33d import design_loop as _dl
    from d33d.evals.failure_capture import (
        default_run_design_loop_hook_sync,
        read_failure_events,
    )

    out = tmp_path / "failures.jsonl"

    class _Result:
        status = "exhausted"
        failure_reason = "empty_model"

        class _Best:
            scad_source = "cube([1,1,1]);"

        best = _Best()

    seen: dict[str, Any] = {}

    async def _fake_real_run(**kwargs):
        seen.update(kwargs)
        return _Result()

    # The REAL hook (the only way to exercise its pop-and-forward logic).
    monkeypatch.setattr(_dl, "run_design_loop_async", _fake_real_run)
    hook = default_run_design_loop_hook_sync(path=out)
    hook(
        photo="data:image/png;base64,x",
        chat_history=("a prior turn about something else",),
        stated_dims=(1.0, 2.0, 3.0),
        render_fn=lambda *a: None,
        llm_fn=lambda *a: None,
        model="model-x",
        prompt_version="deadbeef",
        request=f"Please create {TOKEN}",
    )
    # The loop received the current request (forwarded past the pop).
    assert seen.get("request") == f"Please create {TOKEN}"
    # The archive line carries the SAME value — not the prior-turn history.
    events = read_failure_events(out)
    assert len(events) == 1
    assert events[0].request == f"Please create {TOKEN}"
    assert "a prior turn about something else" not in events[0].request


# ---------------------------------------------------------------------------
# The FINALIZE seam (``_finalize_loop_kwargs`` in d33d/versions_routes.py):
# its ``request`` (body.request or body.message or the "finalize project N"
# placeholder) must reach the loop's new parameter, placeholder included.
# ---------------------------------------------------------------------------


def test_finalize_seam_request_reaches_loop_kwarg(app_with_versions):
    """The FINALIZE route's ``request`` (a third caller path — ``_finalize_
    loop_kwargs`` in ``d33d.versions_routes``) is forwarded to the loop's
    ``request`` parameter (the value ``_design_messages`` renders as the
    first ``Request:`` line), placeholder included. Existing finalize
    behavior (201 on pass, the version params, the non-empty request) is
    unchanged."""
    from tests.versioning.helpers import create_project, run_async
    from tests.versioning.test_design_loop_finalize import _StubResult

    captured: dict[str, Any] = {}

    async def _loop(app, **kwargs):
        captured.update(kwargs)
        return _StubResult("pass", {"W": 10})

    async def _call(client):
        proj = await create_project(client)
        pid = proj["id"]
        app_with_versions.state.run_design_loop = _loop
        return await client.post(
            f"/api/projects/{pid}/finalize",
            json={"request": f"Please create {TOKEN}"},
        )

    r = run_async(app_with_versions, _call)
    assert r.status_code == 201, r.text
    # The finalize seam's request reaches the loop's request kwarg
    # (rendered as the prompt's Request line by the loop itself).
    assert captured.get("request") == f"Please create {TOKEN}"


def test_finalize_seam_placeholder_reaches_loop_kwarg(app_with_versions):
    """A FINALIZE with no body falls back to the "finalize project N"
    placeholder — that placeholder (not an empty string) reaches the
    loop's ``request`` parameter (PM decision: placeholder included)."""
    from tests.versioning.helpers import create_project, run_async
    from tests.versioning.test_design_loop_finalize import _StubResult

    captured: dict[str, Any] = {}

    async def _loop(app, **kwargs):
        captured.update(kwargs)
        return _StubResult("pass", {"W": 10})

    async def _call(client):
        proj = await create_project(client)
        pid = proj["id"]
        app_with_versions.state.run_design_loop = _loop
        return await client.post(f"/api/projects/{pid}/finalize", json={})

    r = run_async(app_with_versions, _call)
    assert r.status_code == 201, r.text
    req = captured.get("request")
    assert isinstance(req, str) and req, "placeholder request must be non-empty"
    assert "finalize project" in req


# ---------------------------------------------------------------------------
# Backward compatibility: a caller that supplies NO request (an old stub /
# direct call) builds the exact legacy prompt — no stray Request line.
# ---------------------------------------------------------------------------


def test_blank_request_renders_no_request_line():
    """``request=''`` (the default) renders NO ``Request:`` line — the
    legacy prompt shape, so existing callers that supply no request are
    unchanged in what their prompt looks like."""
    scad = "x = 20; cube([x, x, x]);\n"
    _result, captured = _run(
        scad_script=[scad],
        request="",
        chat_history=(),
        stated=None,
        bbox=_bbox_ok_all,
    )
    user_text = _user_text(captured, 0)
    assert "Request:" not in user_text
    # The dimensions line is present (issue #120 added a design-state block
    # line before it — the block is an additive, self-contained section, so
    # the dimensions line is no longer the FIRST line; assert its presence
    # and content, not its absolute position).
    assert "Reference dimensions (mm, ground truth):" in user_text
