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

import json as _json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from d33d.config.probes import CapabilityResult
from d33d.config.t1_protocol import parse_t1_tool_call, t1_invoke
from d33d.prompt_hash import canonical_hash
from d33d.response_shape import response_message_shape

__all__ = [
    "Dialect",
    "LLMResult",
    "SenderError",
    "llm_request_body",
    "response_message",
    "response_text",
    "role_tools",
    "send",
    "to_ollama_messages",
]

#: The two image dialects the sender supports.
Dialect = Literal["openai", "ollama"]

RequestFactory = Callable[[dict[str, Any]], Awaitable[Any]]

#: Tool names the T1 protocol advertises to the model — the design loop's
#: logical tools (one per role) — the fence carries the payload regardless.
#: This is the design loop's historical allowlist; ``send`` resolves the
#: called role's own allowlist via ``ROLE_TOOL_NAMES`` (:func:`_tool_names
#: for_role`) so a non-loop role (``question``) is never constrained to the
#: loop's names and never able to emit one.
T1_TOOL_NAMES: tuple[str, ...] = ("emit_design", "emit_critique", "emit_classification")

#: The SINGLE tool name a given role is allowed to emit.  ``send`` enforces
#: this at the sender boundary (a call with the wrong tool name is never
#: returned as a valid :class:`LLMResult`) — the shape-based extraction
#: downstream (``design_loop._scad_from_result``,
#: ``critique_protocol.parse_critique``) is a secondary check, never the
#: primary defense.
ROLE_TOOL_NAMES: dict[str, str] = {
    "design": "emit_design",
    "critique": "emit_critique",
    "classification": "emit_classification",
    "question": "emit_answer",
}

#: The OpenAI function-calling tool definition per role — the native ``tools``
#: array the T0 request carries (the maker closures, 
#: ``design_loop.make_llm_fn`` / ``critique_protocol.make_critique_llm_fn``,
#: attach :func:`role_tools` at T0 so the wire schema matches the name the
#: response-side allowlist (:func:`_validate_tool_name`) enforces).  The shape
#: is structurally equal to the ``DESIGN_TOOLS`` test constant in
#: ``tests/test_design_loop_tiers.py``; this is the production copy.
ROLE_TOOL_SCHEMAS: dict[str, dict[str, Any]] = {
    "emit_design": {
        "type": "function",
        "function": {
            "name": "emit_design",
            "description": "Emit the parametric OpenSCAD",
            "parameters": {
                "type": "object",
                "properties": {
                    "scad": {"type": "string"},
                    # Issue #248: the model's own words about each
                    # parameter it declares in the top variable block —
                    # {name, label, unit, axis?, reason?}. Metadata is
                    # joined by name in d33d.design_state; the SCAD
                    # declarations remain the source of the values.
                    "parameters": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string"},
                                "label": {"type": "string"},
                                "unit": {"type": "string"},
                                "axis": {"type": "string"},
                                "reason": {"type": "string"},
                            },
                            "required": ["name", "label"],
                        },
                    },
                },
            },
        },
    },
    "emit_critique": {
        "type": "function",
        "function": {
            "name": "emit_critique",
            "description": "Emit the structured six-view critique verdict",
            "parameters": {
                "type": "object",
                "properties": {
                    "assessment": {"type": "string"},
                    "views": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "view": {"type": "string"},
                                "ok": {"type": "boolean"},
                            },
                        },
                    },
                },
            },
        },
    },
    "emit_classification": {
        "type": "function",
        "function": {
            "name": "emit_classification",
            "description": "Emit the structured failure classification",
            "parameters": {
                "type": "object",
                "properties": {"classification": {"type": "string"}},
            },
        },
    },
    "emit_answer": {
        "type": "function",
        "function": {
            "name": "emit_answer",
            "description": (
                "Emit the answer to the user's question about the current "
                "design. ``answerable`` is true ONLY if the design-state "
                "block contains every value you need; ``answer`` is the "
                "plain-words answer (each cited value names its provenance) "
                "or empty when not answerable."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "answerable": {"type": "boolean"},
                    "answer": {"type": "string"},
                },
                "required": ["answerable", "answer"],
            },
        },
    },
}


def role_tools(role: str) -> list[dict[str, Any]] | None:
    """The role's native tool definition as a ``tools`` list (T0 wire shape).

    ``None`` when the role has no tool contract (unknown role) — callers
    attach the result at T0 only (the T1 branch of :func:`send` never
    carries a ``tools`` array regardless).  The list is a fresh copy so a
    caller mutating it cannot mutate the module-level schema.
    """
    tool_name = ROLE_TOOL_NAMES.get(role)
    schema = ROLE_TOOL_SCHEMAS.get(tool_name) if tool_name else None
    return [schema] if schema is not None else None


def _tool_names_for_role(role: str) -> list[str]:
    """The tool names the T1 protocol advertises for the role.

    The role's own single name from :data:`ROLE_TOOL_NAMES` — so the T1
    fragment (``available tools: …``) and the response-side allowlist
    (``_validate_tool_name``) always agree, for EVERY registered role, not
    just the design loop's three. An unknown role (no entry in the
    registry) falls back to the loop's historical :data:`T1_TOOL_NAMES`
    tuple: ``send`` only raises for unknown roles on the T2/T3 branch, and
    ``make_llm_fn`` never calls an unregistered role, so this is a
    non-escalating defensive default rather than a bypass (the response
    side rejects the call either way — a bare T1 reply fails the codec,
    and a fenced reply can never carry an unregistered name).
    """
    name = ROLE_TOOL_NAMES.get(role)
    return [name] if name else list(T1_TOOL_NAMES)


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


def _normalize_tool_arguments(args: Any) -> dict[str, Any]:
    """Coerce a T0 tool call's ``function.arguments`` wire value to a dict.

    The OpenAI-compatible production endpoint returns ``arguments`` as a
    JSON-ENCODED STRING (e.g. ``'{"scad": "cube(...);"}'``); other shapes
    (dict, ``None``, an empty/invalid JSON string, JSON that parses to a
    non-dict) are normalized to ``{}`` so the downstream extractor's
    ``isinstance(args, dict)`` check is the only remaining gate.  Never
    raises — a malformed wire shape degrades to empty arguments, and the
    loop's existing empty_scad path handles it.
    """
    if isinstance(args, dict):
        return args
    if not isinstance(args, str):
        return {}
    try:
        parsed = _json.loads(args)
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _validate_tool_name(role: str, call_name: Any) -> None:
    """Raise :class:`SenderError` (``status='error'``) iff the parsed tool
    call's name is not the one the called role is allowed to emit.

    A ``None``/empty name (a response with no usable tool call) is rejected
    the same way — a design role that answered with bare prose, or a
    critique role that emitted ``emit_design``'s name, both fail here before
    ``send`` returns anything the caller could mistake for a valid result.
    """
    expected = ROLE_TOOL_NAMES.get(role)
    if expected is None:
        return  # unknown role: no tool contract to enforce
    if not isinstance(call_name, str) or call_name != expected:
        raise SenderError(
            f"tool_name_mismatch: role {role!r} must emit {expected!r}, "
            f"got {call_name!r}",
            status="error",
        )


def response_message(response: Any) -> dict[str, Any]:
    """The message object from an OpenAI-shaped response body.

    Defensive: a non-dict body, a missing/empty ``choices`` list, a non-dict
    ``choices[0]`` or a non-dict ``message`` raises ``TypeError`` (never a
    raw ``KeyError``/``TypeError``) so callers can classify the failure
    instead of crashing with an unclassified exception.
    """
    data = response.json()
    if not isinstance(data, dict):
        raise TypeError("LLM response body is not a JSON object")
    message = response_message_shape(data)
    if message is None:
        raise TypeError("LLM response has no usable choices[0].message")
    return message


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
    call_params: dict[str, Any] | None = None,
) -> LLMResult:
    """One logical LLM call, dispatched by tier.

    The *logical* request (dialect-converted messages + role + system) is
    hashed **before** any tier-specific framing, so the canonical hash is
    identical for T0 and T1, independent of the resolved model id — that is
    what makes "model A vs model B on the same prompt" diffable from
    ``request_logs``.

    * **T0** — one native request with the role's own tool attached
      (``role_tools(role)``, attached only when the role is registered in
      :data:`ROLE_TOOL_NAMES` — ``None`` otherwise); the model's
      ``tool_calls`` pass through (name + arguments).  Some T0-tiered
      models still reply with a fenced-JSON block in ``content`` (tool_calls
      empty) instead of native calls; the T0 branch parses that block with
      the same :func:`parse_t1_tool_call` codec the T1 path uses and
      synthesizes the call into ``tool_calls`` — malformed blocks (or prose
      without a fenced call) fall through to the empty-tool-call result, the
      caller's ``empty_scad`` path, never an exception.
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
                tool_names=_tool_names_for_role(role),
            )
        except (RuntimeError, KeyError, TypeError, ValueError) as exc:
            # RuntimeError is t1_invoke's own documented failure; KeyError /
            # TypeError / ValueError are the belt-and-suspenders catch for
            # any residual raw shape error in the T1 codec path — every one
            # of these becomes a classified error (request-log write still
            # fires), never an unclassified exception.
            raise SenderError(
                str(exc) or exc.__class__.__name__, status="error"
            ) from exc

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
        # Tool-name allowlist: the parsed T1 call must carry the name this
        # role is allowed to emit, or the response is rejected here — a
        # mismatched name never reaches the caller as a valid result.
        _validate_tool_name(role, call.name)
        tool_calls: tuple[dict[str, Any], ...] = (
            {"name": call.name, "arguments": call.arguments},
        )
        # Raw-response observability (T1): log the fenced raw content and the
        # synthesized tool call so server logs show what the model returned.
        _logging = logging.getLogger(__name__)
        try:
            _logging.debug(
                "raw LLM response (T1) for role=%r model=%r content=%r tool_calls=%s",
                role,
                model_id,
                call.raw,
                _json.dumps([dict(tc) for tc in tool_calls], default=str),
            )
        except (TypeError, ValueError) as exc:
            _logging.warning("T1 raw-response log failed: %s", exc)
        return LLMResult(
            content=call.raw,
            tool_calls=tool_calls,
            prompt_hash=prompt_hash,
            tier=tier,
            status="ok",
            request_body=body,
        )

    # --- T0: native tool calling --------------------------------------------
    # The role's own tool schema (``role_tools(role)``) — not a caller-
    # supplied array — so the request's ``tools`` always matches the name
    # the response-side allowlist enforces below, for every role.
    body = llm_request_body(
        model=model_id,
        messages=messages,
        dialect=dialect,
        supports_vision=capability.vision,
        tools=role_tools(role),
        **params,
    )
    prompt_hash = canonical_hash(role=role, messages=body["messages"], system=system)

    resp = await request_factory(body)
    if not getattr(resp, "is_success", False):
        raise SenderError(
            f"LLM call for role {role!r} failed: HTTP {getattr(resp, 'status', '?')}",
            status="error",
        )
    _logging = logging.getLogger(__name__)
    try:
        msg = response_message(resp)
        raw_calls = msg.get("tool_calls") or []
        tool_calls = tuple(
            {
                "name": tc.get("function", {}).get("name"),
                "arguments": _normalize_tool_arguments(
                    tc.get("function", {}).get("arguments")
                ),
            }
            for tc in raw_calls
            if isinstance(tc, dict)
        )
    except (ValueError, AttributeError, TypeError) as exc:
        # Malformed (non-OpenAI-shaped) body: classified error, never a raw
        # KeyError/TypeError escaping unclassified.
        raise SenderError(
            f"LLM response for role {role!r} was not OpenAI-shaped: {exc}",
            status="error",
        ) from exc
    # Tool-name allowlist: the T0 tool call must carry the name this role is
    # allowed to emit, or the response is rejected here — a mismatched name
    # never reaches the caller as a valid result.
    for tc in tool_calls:
        _validate_tool_name(role, tc.get("name"))
    content = msg.get("content") if isinstance(msg.get("content"), str) else ""
    if not tool_calls and content:
        # Fenced-JSON-in-content fallback: a T0-tiered model (Qwen3.8 over
        # the OpenAI-compatible endpoint) sometimes answers with a fenced
        # JSON tool call in content and an empty tool_calls field.  Reuse
        # the T1 codec's parser (handles ```json and untagged ``` fences,
        # returns None — never raises — on malformed input).  Native
        # tool_calls always win (the fallback only triggers when they are
        # empty/absent); malformed blocks (parser returns None) fall
        # through to the caller's empty_scad path, never a crash; a
        # well-formed block with a name the role may not emit is rejected
        # by the same allowlist as the native path.
        fenced = parse_t1_tool_call(content)
        if fenced is not None:
            expected = ROLE_TOOL_NAMES.get(role)
            if expected is None or fenced.name == expected:
                tool_calls = ({"name": fenced.name, "arguments": fenced.arguments},)
            else:
                # Name-mismatched but well-formed fenced block: same
                # rejection as the native path (the sender boundary is the
                # primary tool-name defense, the downstream shape checks
                # secondary).
                _validate_tool_name(role, fenced.name)

    # Raw-response observability: log content + tool_calls after successful
    # parse so server logs show exactly what the model returned for the
    # design loop.  Best-effort — a logging failure must never break the
    # normal return path.
    try:
        _logging.debug(
            "raw LLM response for role=%r model=%r content=%r tool_calls=%s",
            role,
            model_id,
            content,
            _json.dumps([dict(tc) for tc in tool_calls], default=str),
        )
    except (TypeError, ValueError) as exc:
        _logging.warning("T0 raw-response log failed: %s", exc)

    return LLMResult(
        content=content,
        tool_calls=tool_calls,
        prompt_hash=prompt_hash,
        tier=tier,
        status="ok",
        request_body=body,
        usage=_log_usage(resp),
    )
