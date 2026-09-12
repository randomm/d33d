"""Shared OpenAI response-shape validation helper (issue #21, item 5).

The T1 fenced-JSON codec (:mod:`d33d.config.t1_protocol`) and the
design-loop sender (:mod:`d33d.design_llm`) both extract the assistant
message from an OpenAI-compatible ``/chat/completions`` body.  Before the
extraction in this module they carried two independently-maintained copies
of the same validation ladder (dict body -> non-empty ``choices`` list ->
dict ``choices[0]`` -> dict ``message``) that differed only in failure
policy (return ``None`` vs raise ``TypeError``).  That drift risk is
removed by centralising the shape check here; each caller keeps its own
failure policy on top:

* ``t1_protocol`` keeps the ``None``-then-``RuntimeError`` policy.
* ``design_llm`` keeps the ``TypeError`` policy (with typed messages).

The helper returns ``None`` on any shape violation and never raises for
shape reasons; it does NOT absorb the callers' other divergences
(``resp.ok`` checks, JSON-decode handling, or ``content`` extraction).
"""

from __future__ import annotations

from typing import Any

__all__ = ["response_message_shape"]


def response_message_shape(body: Any) -> dict[str, Any] | None:
    """The ``choices[0].message`` dict of an OpenAI-shaped body, or ``None``.

    Validates the shape ladder only: ``body`` must be a dict, ``choices``
    must be a non-empty list, ``choices[0]`` a dict, and ``choices[0]``
    ``message`` a dict.  Any violation yields ``None`` (the caller applies
    its own failure policy); no raw ``KeyError``/``TypeError`` escapes for
    shape reasons.  ``content`` extraction and HTTP-level concerns (ok /
    json decode) stay with the caller.
    """
    if not isinstance(body, dict):
        return None
    choices = body.get("choices")
    if not isinstance(choices, list) or not choices:
        return None
    first = choices[0]
    if not isinstance(first, dict):
        return None
    message = first.get("message")
    if not isinstance(message, dict):
        return None
    return message
