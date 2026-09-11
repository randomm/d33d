"""Capability-aware LLM sender for the design loop (ticket #5, task-tiers).

The design loop's LLM calls (design, critique, classification) go through a
single sender.  The loop never branches on capability tier: T0 (native tool
calling) and T1 (fenced-JSON text protocol, built from day one) are the same
logical operation at this boundary — only the request/response codec differs
(``d33d.config.t1_protocol`` owns the T1 codec, mirroring ``probes.py`` where
the HTTP edge is injected per call).

What this module owns:

* **T0/T1 dispatch** — the tier comes from the capability probe result
  (``CapabilityResult.tier``), which the caller caches per ``base_url`` +
  model hash via ``CapabilityCache``.  T0 sends the native ``tools=`` request
  in one call; T1 routes through ``t1_invoke`` (fenced-JSON + corrective
  retry).  T2/T3 have no tool channel: :class:`SenderError` with a
  ``no_tools_supported`` status so the loop can route the role to its
  deterministic-gate fallback instead of crashing.
* **Image dialect** — the OpenAI-compatible spectrum is real: Ollama's
  ``/v1`` shim takes an ``images`` array on the message, not OpenAI
  ``image_url`` content parts.  ``dialect="openai"`` keeps image parts inline
  in ``content``; ``dialect="ollama"`` moves every image out of ``content``
  into the sibling ``images`` array (a plain-text ``content`` string).  The
  image-stripping sanitiser runs *before* this module, so its placeholder
  text parts are preserved verbatim.
* **Canonical-hash logging** — every outgoing call hashes its post-dialect
  message shape via ``d33d.prompt_hash.canonical_hash`` (role + messages,
  never the resolved model id) and the caller wires the returned hash to
  ``Connection.log_request``.  Two models called on the same prompt share the
  hash and are diffable purely from ``request_logs``.

The HTTP edge is injected: ``request_factory(request) -> response`` where
``request`` is the full outgoing body and ``response`` mirrors httpx
(``.ok``, ``.json()``).  No real network in this module or its tests.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from d33d.config.probes import CapabilityResult
from d33d.config.t1_protocol import t1_invoke
from d33d.prompt_hash import canonical_hash

__all__ = [
    "Dialect",
    "LLMResult",
    "SenderError",
    "llm_request_body",
    "response_message",
    "response_text",
    "send",
    "to_ollama_messages",
]

#: The two image dialects the sender supports.
Dialect = Literal["openai", "ollama"]

RequestFactory = Callable[[dict[str, Any]], Awaitable[Any]]

#: Tool names the T1 protocol advertises to the model — the design loop's
#: logical tools (one per role) — the fence carries the payload regardless.
T1_TOOL_NAMES: tuple[str, ...] = ("emit_design", "emit_critique", "emit_classification")


class SenderError(RuntimeError):
    """The endpoint answered non-OK, or the tier has no tool channel.

    ``status`` is the value the caller records in ``request_logs``:
    ``'error'`` (HTTP failure / T1 retry exhaustion) or
    ``'no_tools_supported'`` (T2/T3 — the loop degrades the role to
    deterministic-gate-only scoring on this).
    """

    def __init__(self, message: str, *, status: str) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class LLMResult:
    """One completed logical LLM call, identical in shape across tiers."""

    content: str
    tool_calls: tuple[dict[str, Any], ...]
    prompt_hash: str
    tier: str
    status: str
    request_body: dict[str, Any]
    usage: dict[str, int] = field(default_factory=dict)


def response_message(response: Any) -> dict[str, Any]:
    """The message object from an OpenAI-shaped response body."""
    return response.json()["choices"][0]["message"]


def response_text(response: Any) -> str:
    """The message content as a string ('' when the model returned null)."""
    content = response_message(response).get("content")
    return content if isinstance(content, str) else ""


def to_ollama_messages(
    messages: list[dict[str, Any]], *, supports_vision: bool = True
) -> list[dict[str, Any]]:
    """Convert OpenAI-shaped messages to the Ollama ``/v1`` images-array form.

    OpenAI: a message ``content`` is a list of parts; an image is the part
    ``{"type": "image_url", "image_url": {"url": ...}}``.  Ollama's shim: the
    message carries an ``images`` array of data URIs alongside a plain-string
    ``content``.  Every ``image_url`` part is moved out of ``content`` into
    ``images`` in order; all other parts are kept.  When the content held
    parts it becomes a joined string — Ollama's ``content`` is a string, not a
    list.

    ``supports_vision=False`` drops the images entirely (the image-stripping
    sanitiser runs before this call, so any ``[image removed ...]``
    placeholder text parts are preserved verbatim).
    """
    out: list[dict[str, Any]] = []
    for msg in messages:
        m = dict(msg)
        content = m.get("content")
        if isinstance(content, list):
            texts: list[str] = []
            images: list[str] = []
            for part in content:
                if (
                    isinstance(part, dict)
                    and part.get("type") == "image_url"
                    and supports_vision
                ):
                    url = part.get("image_url", {}).get("url")
                    if url:
                        images.append(url)
                elif isinstance(part, dict) and part.get("type") == "text":
                    text = part.get("text")
                    if text:
                        texts.append(text)
                elif isinstance(part, str) and part:
                    texts.append(part)
            m["content"] = "\n".join(texts)
            if images:
                m["images"] = images
        out.append(m)
    return out


def llm_request_body(
    *,
    model: str,
    messages: list[dict[str, Any]],
    dialect: Dialect = "openai",
    supports_vision: bool = True,
    tools: list[dict[str, Any]] | None = None,
    **call_params: Any,
) -> dict[str, Any]:
    """Build the full outgoing request body (pre-hash, per dialect/tier).

    ``dialect='ollama'`` converts the messages to the images-array form.
    ``tools`` attach only when non-empty (the T0 native path); the T1 path
    passes none — the fenced-JSON block is the tool channel.  ``call_params``
    carry the override-cascade output (temperature, max_tokens, ...); None
    values are dropped so the body matches what would be sent.
    """
    msgs = (
        to_ollama_messages(messages, supports_vision=supports_vision)
        if dialect == "ollama"
        else messages
    )
    body: dict[str, Any] = {"model": model, "messages": msgs}
    for key, value in call_params.items():
        if value is not None:
            body[key] = value
    if tools:
        body["tools"] = list(tools)
    return body


def _log_usage(response: Any) -> dict[str, int]:
    """Best-effort token usage from a response body ({} on any failure)."""
    try:
        usage = response.json().get("usage") or {}
    except (AttributeError, ValueError, TypeError, KeyError):
        return {}
    return {
        "prompt_tokens": int(usage.get("prompt_tokens", 0) or 0),
        "completion_tokens": int(usage.get("completion_tokens", 0) or 0),
    }


def _last_user_text(messages: list[dict[str, Any]]) -> str:
    """The last user message's text (the T1 protocol's single-turn input).

    Handles both the plain-string form (Ollama dialect) and the OpenAI
    multipart form (text parts joined with newlines).  Pure-image messages
    yield '' — the T1 protocol degrades to an empty user turn, which is the
    documented contract for a vision-only prompt over a text protocol.
    """
    for msg in reversed(messages):
        if msg.get("role") != "user":
            continue
        content = msg.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = [
                p.get("text", "")
                for p in content
                if isinstance(p, dict) and p.get("type") == "text"
            ]
            return "\n".join(p for p in parts if p)
    return ""


async def send(
    *,
    role: str,
    model_id: str,
    messages: list[dict[str, Any]],
    request_factory: RequestFactory,
    capability: CapabilityResult,
    dialect: Dialect = "openai",
    system: str | None = None,
    tools: list[dict[str, Any]] | None = None,
    call_params: dict[str, Any] | None = None,
) -> LLMResult:
    """One logical LLM call, dispatched by tier.

    The *logical* request (dialect-converted messages + role + system) is
    hashed **before** any tier-specific framing, so the canonical hash is
    identical for T0 and T1, independent of the resolved model id — that is
    what makes "model A vs model B on the same prompt" diffable from
    ``request_logs``.

    * **T0** — one native request with ``tools`` attached when provided; the
      model's ``tool_calls`` pass through (name + arguments).
    * **T1** — ``t1_invoke`` (fenced-JSON codec + corrective retry).  The
      parsed block is synthesized into a ``tool_calls`` tuple so callers
      treat T0 and T1 identically; the raw fenced response stays in
      ``content``.
    * **T2/T3** — :class:`SenderError`` (``status='no_tools_supported'``);
      the loop degrades the role to deterministic-gate-only scoring.

    On a non-OK response the error carries ``status='error'``; the caller
    still logs the row (hash + status) via ``Connection.log_request``.
    """
    tier = capability.tier
    params = dict(call_params or {})

    if tier in ("T2", "T3"):
        raise SenderError(
            f"role {role!r}: tier {tier} has no tool channel "
            "(degrade to deterministic-gate scoring)",
            status="no_tools_supported",
        )

    if tier == "T1":
        params_clean = {k: v for k, v in params.items() if v is not None}

        async def envelope_factory(envelope: dict[str, Any]):
            req = {
                "model": model_id,
                "messages": list(envelope["messages"]),
            }
            req.update(params_clean)
            return await request_factory(req)

        try:
            call = await t1_invoke(
                request_factory=envelope_factory,
                system_prompt=system or "",
                user_message=_last_user_text(messages),
                tool_names=list(T1_TOOL_NAMES),
            )
        except RuntimeError as exc:
            raise SenderError(str(exc), status="error") from exc

        # The reported request_body is the dialect-converted logical shape
        # (what goes on the wire); the T1 codec's internal envelope (system
        # fragment + corrective retries) is not the wire shape.
        body = llm_request_body(
            model=model_id,
            messages=messages,
            dialect=dialect,
            supports_vision=capability.vision,
            tools=None,
            **params,
        )
        prompt_hash = canonical_hash(
            role=role, messages=body["messages"], system=system
        )
        tool_calls: tuple[dict[str, Any], ...] = (
            {"name": call.name, "arguments": call.arguments},
        )
        return LLMResult(
            content=call.raw,
            tool_calls=tool_calls,
            prompt_hash=prompt_hash,
            tier=tier,
            status="ok",
            request_body=body,
        )

    # --- T0: native tool calling --------------------------------------------
    body = llm_request_body(
        model=model_id,
        messages=messages,
        dialect=dialect,
        supports_vision=capability.vision,
        tools=tools if tier == "T0" else None,
        **params,
    )
    prompt_hash = canonical_hash(role=role, messages=body["messages"], system=system)

    resp = await request_factory(body)
    if not getattr(resp, "ok", False):
        raise SenderError(
            f"LLM call for role {role!r} failed: HTTP {getattr(resp, 'status', '?')}",
            status="error",
        )
    msg = resp.json()["choices"][0]["message"]
    raw_calls = msg.get("tool_calls") or []
    tool_calls = tuple(
        {
            "name": tc.get("function", {}).get("name"),
            "arguments": tc.get("function", {}).get("arguments"),
        }
        for tc in raw_calls
    )
    content = msg.get("content")
    return LLMResult(
        content=content if isinstance(content, str) else "",
        tool_calls=tool_calls,
        prompt_hash=prompt_hash,
        tier=tier,
        status="ok",
        request_body=body,
        usage=_log_usage(resp),
    )
