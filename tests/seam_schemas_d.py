"""SEAM D — SSE adapter -> browser frame schemas (issue #102).

The wire contract is the ``(kind, data)`` tuple
``d33d.design_loop_events.run_design_loop_with_events`` yields:

- ``"progress"`` — ``{"step": ...}``; the version-created frame carries
  ``step``/``version_id`` (ALWAYS) plus ``stl_data_uri`` / ``views``
  (OMITTED, never null, when the durable bytes are unreadable — the
  omit-not-null policy) plus ``bbox_abstained`` (always present on
  pass frames — the ticket #91 field the stale spec inventory omitted);
- ``"token"`` — ``{"text": ...}`` (the sole owner of the SCAD source);
- ``"done"`` — ``{"message": ...}`` (+ ``bbox_abstained`` on passes);
- ``"error"`` — ``{"message": ...}`` (+ ``reason`` when the loop
  produced a structured failure reason, omitted otherwise).

The version-created field set is DERIVED from the LIVE frame
construction (an AST walk of ``run_design_loop_with_events``) —
``step`` and ``bbox_abstained`` included — never from a hand-written
list (PM decision, issue #102: the spec's SEAM D inventory was stale).
The schema asserts presence-or-absence per the omit policy, NOT
unconditional presence.

The browser is NOT drivable from a test: replay asserts frame SHAPE
(kind tag + data-field presence/absence), not rendering.
"""

from __future__ import annotations

import ast
import inspect
from typing import Any

from tests.seam_schemas import SeamError

__all__ = [
    "FRAME_KINDS",
    "validate_frame",
    "validate_frames_stream",
    "version_created_frame_fields",
]

#: The closed frame-kind set (the SSE contract's ``event`` names).
FRAME_KINDS: frozenset[str] = frozenset({"progress", "token", "done", "error"})


def _frame_dict_literals() -> list[tuple[ast.Name, ast.Dict]]:
    """The ``name: dict = {...}`` literal initialisers in
    ``run_design_loop_with_events`` — e.g. ``vc_frame`` (``step`` +
    ``version_id``) and ``error_data`` (``message``)."""
    from d33d import design_loop_events

    tree = ast.parse(inspect.getsource(design_loop_events.run_design_loop_with_events))
    out: list[tuple[ast.Name, ast.Dict]] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and isinstance(node.value, ast.Dict)
        ):
            out.append((node.target, node.value))
    return out


def version_created_frame_fields() -> set[str]:
    """The version-created progress frame's field set, DERIVED from the
    live ``run_design_loop_with_events`` construction:

    * the ``vc_frame: dict = {...}`` literal's keys (``step``,
      ``version_id``);
    * every ``frame_fields["x"] = ...`` subscript key (``stl_data_uri``,
      ``views`` — the omit-policy-conditional fields);
    * every ``vc_frame["x"] = ...`` subscript key (``bbox_abstained`` —
      the always-present ticket #91 flag).

    Including ``step`` and ``bbox_abstained``: the stale spec inventory
    omitted both, and a hand-written list is the same rot in a new place.
    """
    from d33d import design_loop_events

    tree = ast.parse(inspect.getsource(design_loop_events.run_design_loop_with_events))
    fields: set[str] = set()
    for name, d in _frame_dict_literals():
        if name.id == "vc_frame":
            for k in d.keys:
                if isinstance(k, ast.Constant):
                    fields.add(str(k.value))
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Assign)
            and isinstance(node.targets[0], ast.Subscript)
            and isinstance(node.targets[0].value, ast.Name)
            and node.targets[0].value.id in ("vc_frame", "frame_fields")
            and isinstance(node.targets[0].slice, ast.Constant)
        ):
            fields.add(str(node.targets[0].slice.value))
    return fields


def validate_frame(frame: tuple[Any, Any]) -> tuple[str, dict[str, Any]]:
    """Assert one ``(kind, data)`` frame matches the declared shape.

    * kind is one of the four (``progress`` / ``token`` / ``done`` /
      ``error``);
    * progress: ``step`` present, a string; on the version-created
      frame (``step == "version-created"``) the DERIVED field set is
      presence-or-absent per the omit policy — every derived field that
      is present carries a JSON-safe value of the right type, and
      ``stl_data_uri`` / ``views`` are never null (absent instead);
    * token: ``text`` present, a string;
    * done: ``message`` present, a string;
    * error: ``message`` present, a string; ``reason`` present (str) iff
      the loop produced a structured reason (absent otherwise, never
      null).

    Returns ``(kind, data)`` unchanged on success (so callers can chain).
    """
    if not isinstance(frame, tuple) or len(frame) != 2:
        raise SeamError(
            "D", f"frame is {type(frame).__name__}, expected a (kind, data) tuple"
        )
    kind, data = frame
    if not isinstance(kind, str) or kind not in FRAME_KINDS:
        raise SeamError("D", f"frame kind {kind!r} not one of {sorted(FRAME_KINDS)}")
    if not isinstance(data, dict):
        raise SeamError("D", f"{kind} frame data is {type(data).__name__}, expected dict")

    if kind == "progress":
        step = data.get("step")
        if not isinstance(step, str) or not step:
            raise SeamError("D", "progress frame 'step' missing or not a string")
        if step == "version-created":
            derived = version_created_frame_fields()
            # step is the discriminator itself: always present (checked
            # above). The remaining derived fields must be JSON-safe when
            # present (never null — the omit policy omits, never nulls).
            for name in derived - {"step"}:
                if name not in data:
                    continue  # omit-policy-conditional: absence is valid
                value = data[name]
                if value is None:
                    raise SeamError(
                        "D",
                        f"version-created frame field {name!r} is null — the "
                        "omit policy omits the field entirely, never emits null",
                    )
            if data.get("version_id") is None:
                raise SeamError("D", "version-created frame 'version_id' is missing")
            if not isinstance(data["version_id"], int):
                raise SeamError("D", "version-created frame 'version_id' is not an int")
            if "stl_data_uri" in data and not (
                isinstance(data["stl_data_uri"], str)
                and data["stl_data_uri"].startswith("data:")
            ):
                raise SeamError(
                    "D", "version-created frame 'stl_data_uri' is not a data URI string"
                )
            if "views" in data:
                views = data["views"]
                if not isinstance(views, dict) or not views:
                    raise SeamError("D", "version-created frame 'views' must be a non-empty dict")
                for name, uri in views.items():
                    if not (isinstance(uri, str) and uri.startswith("data:image/png;base64,")):
                        raise SeamError(
                            "D",
                            f"version-created frame views[{name!r}] is not a PNG data URI",
                        )
            if "bbox_abstained" in data and not isinstance(data["bbox_abstained"], bool):
                raise SeamError("D", "version-created frame 'bbox_abstained' is not a bool")
        return frame

    if kind == "token":
        if not isinstance(data.get("text"), str):
            raise SeamError("D", "token frame 'text' missing or not a string")
        return frame

    if kind == "done":
        if not isinstance(data.get("message"), str):
            raise SeamError("D", "done frame 'message' missing or not a string")
        return frame

    # error
    if not isinstance(data.get("message"), str):
        raise SeamError("D", "error frame 'message' missing or not a string")
    if "reason" in data:
        if data["reason"] is None:
            raise SeamError(
                "D", "error frame 'reason' is null — the omit policy omits it, never emits null"
            )
        if not isinstance(data["reason"], str):
            raise SeamError("D", "error frame 'reason' present but not a string")
    return frame


def validate_frames_stream(frames: list[tuple[Any, Any]]) -> list[tuple[str, dict[str, Any]]]:
    """Assert a full frame stream: every frame valid AND the stream
    terminates with a terminal ``done`` or ``error`` frame (the adapter's
    contract — the client resolves only on a terminal frame)."""
    out = [validate_frame(f) for f in frames]
    if not out:
        raise SeamError("D", "empty frame stream — no terminal frame")
    if out[-1][0] not in ("done", "error"):
        raise SeamError(
            "D", f"stream ends with {out[-1][0]!r}, expected a terminal done|error frame"
        )
    return out
