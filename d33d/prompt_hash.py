"""Prompt canonical hash for request_logs diffing (issue #3, acceptance #5).

The canonical hash is the join key that lets two ``request_logs`` rows —
one per model, called on the *same prompt* — be diffed purely from the
logs. Its input is the canonical form of the outgoing request:

* the job ``role`` (design / critique / classification / ...),
* the system prompt (optional),
* the message list (roles, content, tool calls, image placeholders).

It EXCLUDES the resolved model id and the provider. Including either would
make the hash differ between model A and model B and destroy its purpose:
the whole point is that *identical* prompts hash *identically* regardless
of which model they were sent through.

Normalisation rules (each one keeps a semantically-identical prompt on the
same hash, and each is pinned by a test):

* whitespace runs collapse to a single space in text content;
* JSON object key order is normalised (sorted keys, compact separators);
* content-part *order* inside a single message is normalised (multi-part
  lists are sorted by their JSON serialisation, so ``[text, image]`` and
  ``[image, text]`` hash the same);
* ``tool_calls[].function.arguments`` (a JSON string) is re-serialised in
  canonical form so key-order drift in the tool arguments does not split
  the hash.

Everything else is hashed verbatim: role names, non-text content, tool
names, tool_call ids, the image URL (which is the reference-photo identity
that the diff is supposed to compare).
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

_WS_RUN = re.compile(r"\s+")


def _canonical_value(value: Any) -> Any:
    """Recursively normalise a content tree into a canonical form.

    Strings that are valid JSON objects/arrays are re-serialised in
    canonical form (sorted keys, compact separators) so that key-order
    drift in embedded JSON does not split the hash. Strings that are not
    valid JSON (plain prose, image URLs, etc.) get whitespace collapsed.
    """
    if isinstance(value, str):
        # First try to parse as JSON; if it's an object or array, re-serialise.
        stripped = value.strip()
        if stripped and stripped[0] in "[{":
            try:
                parsed = json.loads(stripped)
                if isinstance(parsed, (dict, list)):
                    return json.dumps(
                        _canonical_value(parsed), sort_keys=True, separators=(",", ":")
                    )
            except (json.JSONDecodeError, ValueError):
                pass
        # Fall through: plain text — collapse whitespace runs.
        return _WS_RUN.sub(" ", value).strip()
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for k in value:
            out[str(k)] = _canonical_value(value[k])
        # Sort keys so {"a":1,"b":2} and {"b":2,"a":1} are equal.
        return {k: out[k] for k in sorted(out)}
    if isinstance(value, list):
        return [_canonical_value(v) for v in value]
    # int / float / bool / None pass through untouched.
    return value


def _canonical_message(msg: dict[str, Any]) -> dict[str, Any]:
    c = dict(msg)
    c["role"] = _WS_RUN.sub(" ", str(c.get("role", ""))).strip()
    c["content"] = _canonical_content(c.get("content"))
    tc = c.get("tool_calls")
    if isinstance(tc, list):
        new_tc = []
        for call in tc:
            call = dict(call)
            fn = call.get("function")
            if isinstance(fn, dict):
                fn = dict(fn)
                args = fn.get("arguments")
                if isinstance(args, str):
                    try:
                        parsed = json.loads(args)
                        if isinstance(parsed, dict):
                            # Canonical: sorted keys, compact separators.
                            fn["arguments"] = json.dumps(
                                parsed, sort_keys=True, separators=(",", ":")
                            )
                    except (json.JSONDecodeError, ValueError):
                        pass  # keep raw string as-is (already normalised below)
            c_fn: dict[str, Any] = dict(fn) if fn is not None else {}
            for k in ("name", "arguments"):
                if k in c_fn:
                    c_fn[k] = _WS_RUN.sub(" ", str(c_fn[k])).strip()
            call["function"] = c_fn
            call["id"] = _WS_RUN.sub(" ", str(call.get("id", ""))).strip()
            new_tc.append(call)
        c["tool_calls"] = new_tc
    if "tool_call_id" in c:
        c["tool_call_id"] = _WS_RUN.sub(" ", str(c["tool_call_id"])).strip()
    return c


def _canonical_content(content: Any) -> Any:
    if content is None:
        return None
    if isinstance(content, str):
        return _canonical_value(content)
    if isinstance(content, list):
        parts = [_canonical_value(p) for p in content]
        # Sort content-part order so [text, image] == [image, text].
        parts.sort(key=lambda p: json.dumps(p, sort_keys=True, default=str))
        return parts
    return _canonical_value(content)


def canonical_hash(
    *,
    role: str,
    messages: list[dict[str, Any]],
    system: str | None = None,
) -> str:
    """Compute the prompt canonical hash (SHA-256, hex).

    Args:
        role: The job role that was called (``design``, ``critique``,
            ``classification``, ...). Excluded from the input is the
            *model* — included here is the *role* because two calls on
            different roles are semantically different prompts even if the
            messages look similar.
        messages: The outgoing message list (post-sanitisation). This is
            what the model actually saw, which is what we want to diff.
        system: Optional system prompt string. Included if non-None.

    Returns:
        A 64-character hex SHA-256 digest. Stable across calls, across
        platforms (no set-iteration-order, no wall-clock, no pid).
    """
    payload: dict[str, Any] = {
        "role": _WS_RUN.sub(" ", str(role)).strip(),
        "system": _WS_RUN.sub(" ", system).strip() if system is not None else None,
        "messages": [_canonical_message(m) for m in messages],
    }
    serialised = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(serialised.encode("utf-8")).hexdigest()
