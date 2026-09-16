"""SEAM sentinel — the rendered prompt must carry the user's request
(issue #102, the #97 shape).

The #97 defect: the user's request was threaded through six hops and
never rendered into the prompt — the model was never asked, and a
passing design was produced for a request it never saw. This test
places a UNIQUE, IMPROBABLE token in the user's request and asserts it
appears in the FULLY-RENDERED user text as it leaves ``_design_messages``
(post-formatting, pre-sender) — the exact string the sender would put on
the wire. The test must fail if the request stops being rendered (a
regression to the #97 shape).

This is distinct from the existing #97 test (``test_issue97_request_in_
prompt.py``): that test captures the prompt via a fake ``llm_fn`` (the
loop's captured ``messages``), but this test asserts on the ``_design_
messages`` output directly — the post-formatting, pre-sender string,
so any loss between the ``request`` parameter and the wire-bound user
text (including a codec re-framing) is caught.
"""

from __future__ import annotations

from typing import Any

from d33d.design_loop import _design_messages

# A unique, improbable token that appears NOWHERE in the system prompt,
# the dimensions line, the emission instruction, or the repair directive
# — its presence in the rendered user text proves the request was
# rendered. The token is deliberately unguessable (a UUID-like string)
# so it cannot accidentally appear in any other part of the prompt.
SENTINEL_TOKEN = "xk7q2m9z-rendertest-4401"


def _rendered_user_text(
    *,
    photo: str = "data:image/png;base64,REF",
    chat_history: tuple[str, ...] = (),
    stated: tuple[float, float, float] = (0.0, 0.0, 0.0),
    repair: dict[str, Any] | None = None,
    request: str = "",
) -> str:
    """The FULLY-RENDERED user text as it leaves ``_design_messages``
    (post-formatting, pre-sender) — the exact string the sender would
    put on the wire. The ``_design_messages`` return is a list of one
    user message whose ``content`` is a list of parts (text + image);
    the text part is the rendered prompt."""
    messages = _design_messages(
        photo=photo,
        chat_history=chat_history,
        stated=stated,
        repair=repair,
        request=request,
    )
    assert len(messages) == 1, f"expected 1 message, got {len(messages)}"
    msg = messages[0]
    assert msg["role"] == "user"
    parts = msg["content"]
    text_parts = [p for p in parts if p.get("type") == "text"]
    assert len(text_parts) == 1, f"expected 1 text part, got {len(text_parts)}"
    return text_parts[0]["text"]


def test_sentinel_token_appears_in_rendered_user_text():
    """The decisive test (the #97 shape): a unique, improbable token in
    the user's request MUST appear in the fully-rendered user text as it
    leaves ``_design_messages`` (post-formatting, pre-sender). If the
    request stops being rendered (a regression to the #97 shape — the
    value threaded through six hops but never rendered), this test fails
    LOUDLY."""
    request = f"Please create {SENTINEL_TOKEN} as a 20mm cube"
    rendered = _rendered_user_text(request=request)
    # The token is in the RENDERED user text (the decisive assertion).
    assert SENTINEL_TOKEN in rendered, (
        f"sentinel token {SENTINEL_TOKEN!r} missing from rendered user text — "
        f"the #97 shape (the request was threaded but never rendered):\n{rendered}"
    )
    # The token is on the Request: line (the first line, per the #97 fix).
    assert rendered.startswith(f"Request: {request}"), (
        f"the Request: line must be the first line and carry the full request:\n{rendered}"
    )


def test_sentinel_token_in_repair_iteration_rendered_text():
    """The repair iteration (iteration 2) ALSO renders the request
    verbatim in its user text — the repair block alone is not enough to
    steer toward what was asked (the #97 invariant holds for every
    iteration, not just the first)."""
    request = f"Please create {SENTINEL_TOKEN} as a 20mm cube"
    repair = {
        "failure_class": "syntax_error",
        "instruction": "fix the trailing semicolon",
        "scad_source": "cube([20]);",
    }
    rendered = _rendered_user_text(request=request, repair=repair)
    # The token is in the RENDERED user text (the repair iteration).
    assert SENTINEL_TOKEN in rendered, (
        f"sentinel token missing from repair-iteration rendered text:\n{rendered}"
    )
    # The request precedes the repair block (it is the first line).
    assert rendered.startswith(f"Request: {request}")
    assert "REPAIR directive" in rendered


def test_sentinel_token_absent_when_request_empty():
    """A blank request renders NO ``Request:`` line (the legacy shape —
    a caller that supplies no request builds the exact prompt it always
    built). The sentinel token is NOT in the rendered text (there is no
    request to render)."""
    rendered = _rendered_user_text(request="")
    assert "Request:" not in rendered, (
        f"an empty request must render no Request: line:\n{rendered}"
    )
    assert SENTINEL_TOKEN not in rendered


def test_sentinel_token_not_in_system_prompt():
    """The sentinel token must NOT appear in the system prompt (the
    system prompt is built by ``_design_system`` — it carries the
    dimensions, not the request). The token is only in the user text."""
    from d33d.design_loop import _design_system

    system = _design_system((20.0, 0.0, 0.0))
    assert SENTINEL_TOKEN not in system, (
        f"the sentinel token must not appear in the system prompt:\n{system}"
    )


def test_sentinel_token_captured_by_llm_fn_matches_rendered_text():
    """End-to-end: the token captured by a fake ``llm_fn`` (the loop's
    captured ``messages``) matches the ``_design_messages`` rendered text
    (the sender's wire-bound string). This proves the loop's ``_design_
    messages`` output is what the sender would send — no loss between the
    rendered text and the wire."""
    from d33d.design_loop import run_design_loop
    from d33d.render_worker import RenderResult

    scad = "x = 20;\ncube([x, x, x]);\n"
    captured: list[list[dict[str, Any]]] = []
    i = {"n": 0}

    async def llm_fn(role, messages, system):
        captured.append(messages)
        from d33d.design_llm import LLMResult

        return LLMResult(
            content="",
            tool_calls=({"name": "emit_design", "arguments": {"scad": scad}},),
            prompt_hash="h" * 64,
            tier="T1",
            status="ok",
            request_body={},
        )

    async def render_fn(scad_source, defines):
        i["n"] += 1
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

    from d33d.design_loop import BboxInfo

    run_design_loop(
        photo="data:image/png;base64,REF",
        stated_dims=(20.0, 0.0, 0.0),
        render_fn=render_fn,
        llm_fn=llm_fn,
        bbox_fn=lambda r: BboxInfo(x=20.0, y=20.0, z=20.0, volume=8000.0),
        request=f"Please create {SENTINEL_TOKEN} as a 20mm cube",
        chat_history=(),
        max_iterations=1,
    )
    # The llm_fn captured the messages (the loop's _design_messages
    # output). The token is in the captured user text.
    assert captured, "no llm_fn call captured"
    msg = captured[0][0]
    parts = msg["content"]
    text = [p for p in parts if p.get("type") == "text"]
    assert len(text) == 1
    # The token is in the captured (wire-bound) user text.
    assert SENTINEL_TOKEN in text[0]["text"], (
        f"sentinel token missing from captured (wire-bound) user text:\n{text[0]['text']}"
    )
    # The captured text matches the _design_messages rendered text
    # (the sender's wire-bound string — no loss between the two).
    rendered_direct = _rendered_user_text(
        request=f"Please create {SENTINEL_TOKEN} as a 20mm cube",
        stated=(20.0, 0.0, 0.0),
    )
    assert text[0]["text"] == rendered_direct, (
        "the captured (wire-bound) user text differs from _design_messages output — "
        "a loss between the rendered text and the wire"
    )
