"""SEAM A — LLM -> loop stage-boundary schemas (issue #102).

The wire payload at this seam is what ``d33d.design_llm.send`` returns:
an :class:`LLMResult` whose ``tool_calls`` entries carry ``name`` +
``arguments``. The production endpoint returns ``function.arguments``
as a JSON-ENCODED STRING (issue #80); the normalisation
(``_normalize_tool_arguments``) must turn that into a dict BEFORE the
loop's extractor (``_scad_from_result``) sees it. The second real
production shape (issue #79) is a T0-tiered model that answers with a
fenced-JSON tool call in ``content`` and an EMPTY ``tool_calls`` — after
``send`` synthesizes it, the same shape applies.

Field sets are DERIVED from ``dataclasses.fields(LLMResult)`` — never a
hand-written list (a hand-maintained list is the same rot in a new
place, the defect class issue #102 exists to remove).
"""

from __future__ import annotations

import dataclasses
import os
from typing import Any

from d33d.design_llm import LLMResult

__all__ = [
    "SeamError",
    "declared_field_names",
    "validate_iteration_record_seam_c",
    "validate_llm_result_seam_a",
    "validate_render_result_seam_b",
]


class SeamError(AssertionError):
    """A stage-boundary payload that does not match the declared shape.

    ``seam`` names the boundary (A/B/C/D); ``detail`` names the offending
    field. Tests (and, if enabled, the opt-in runtime hook) fail LOUDLY
    on a ``SeamError`` — a payload that does not match the declared shape
    is the exact defect class #80/#79/#89/#93 shipped.
    """

    def __init__(self, seam: str, detail: str) -> None:
        self.seam = seam
        self.detail = detail
        super().__init__(f"SEAM {seam}: {detail}")


def declared_field_names(cls: type) -> frozenset[str]:
    """The declared ``dataclasses.fields`` names of ``cls`` (derived, never
    hand-maintained). Used by every seam schema so a field rename or
    deletion fails the schema test loudly instead of silently drifting."""
    return frozenset(f.name for f in dataclasses.fields(cls))


def _validate_tool_call_entry(seam_detail_prefix: str, tc: Any) -> None:
    """One ``tool_calls`` entry: a dict whose ``name`` is a string and whose
    ``arguments`` is a DICT after normalisation (issue #80's invariant: a
    JSON-string ``arguments`` that survived the normaliser reaches the loop
    and silently extracts empty SCAD)."""
    if not isinstance(tc, dict):
        raise SeamError("A", f"tool_calls entry is {type(tc).__name__}, expected dict")
    if "name" not in tc:
        raise SeamError("A", "tool_calls entry missing 'name' field")
    if not isinstance(tc["name"], str):
        raise SeamError("A", f"tool_calls entry 'name' is {type(tc['name']).__name__}, expected str")
    if "arguments" not in tc:
        raise SeamError("A", "tool_calls entry missing 'arguments' field")
    if not isinstance(tc["arguments"], dict):
        raise SeamError(
            "A",
            f"tool_call arguments for {tc['name']!r} is "
            f"{type(tc['arguments']).__name__}, expected dict after "
            "normalisation (issue #80: the endpoint returns a JSON string — "
            "re-introducing the un-normalised shape goes RED here)",
        )


def validate_llm_result_seam_a(result: Any) -> LLMResult:
    """Assert an :class:`LLMResult` (post-``send`` normalisation) matches
    the declared shape. Covers BOTH real production shapes:

    * T0 native tool_call whose ``arguments`` was a JSON STRING — after
      ``_normalize_tool_arguments`` it MUST be a dict (issue #80);
    * T1 / fenced-JSON-in-content with originally-empty ``tool_calls`` —
      ``send`` synthesizes the call into ``tool_calls`` with dict
      arguments (issue #79), so the same assertions apply.

    Every declared ``LLMResult`` field is present (the dataclass
    constructor enforces presence; this guard additionally checks the
    value types the loop's extractor and prompt-log hook rely on).
    Returns the result unchanged on success (so callers can chain).
    """
    if not isinstance(result, LLMResult):
        raise SeamError(
            "A",
            f"payload is {type(result).__name__}, expected LLMResult "
            f"(the seam-A payload is send()'s return value)",
        )
    declared = declared_field_names(LLMResult)
    for name in declared:
        if not hasattr(result, name):
            raise SeamError("A", f"declared LLMResult field {name!r} missing")
    if not isinstance(result.content, str):
        raise SeamError("A", f"content is {type(result.content).__name__}, expected str")
    if not isinstance(result.tool_calls, tuple):
        raise SeamError("A", f"tool_calls is {type(result.tool_calls).__name__}, expected tuple")
    for tc in result.tool_calls:
        _validate_tool_call_entry("tool_call", tc)
    if not isinstance(result.prompt_hash, str) or not result.prompt_hash:
        raise SeamError("A", "prompt_hash must be a non-empty str (the request_logs join key)")
    if not isinstance(result.tier, str) or result.tier not in ("T0", "T1", "T2", "T3"):
        raise SeamError("A", f"tier {result.tier!r} not one of T0..T3")
    if not isinstance(result.status, str) or result.status not in ("ok", "error", "no_tools_supported"):
        raise SeamError("A", f"status {result.status!r} not a declared status")
    if not isinstance(result.request_body, dict):
        raise SeamError("A", f"request_body is {type(result.request_body).__name__}, expected dict")
    if not isinstance(result.usage, dict):
        raise SeamError("A", f"usage is {type(result.usage).__name__}, expected dict")
    return result


# ---------------------------------------------------------------------------
# SEAM B — render worker -> loop: RenderResult
# ---------------------------------------------------------------------------


def _is_tempdir_path(path: str, extra_roots: tuple[str, ...] = ()) -> bool:
    """Structural temp-dir test: the path string starts with the system
    tempdir root or one of ``extra_roots`` (e.g. a normalised fixture
    prefix). Deliberately NOT ``Path.exists()`` — durability is asserted
    structurally so the check holds in a fresh checkout where the file
    does not yet exist (PM decision, issue #102)."""
    import tempfile

    roots = [tempfile.gettempdir(), *(str(r) for r in extra_roots)]
    for root in roots:
        if path == root or path.startswith(root + os.sep):
            return True
    return False


def validate_render_result_seam_b(
    result: Any, *, temp_roots: tuple[str, ...] = ()
) -> Any:
    """Assert a ``RenderResult`` (the ``result.json`` payload, the seam-B
    wire shape) matches the declared shape.

    * every declared field is present (derived from
      ``dataclasses.fields(RenderResult)`` — never a hand-written list);
    * when ``render_artifact_dir`` is set, ``stl`` and every ``views``
      entry are DURABLE paths — structurally, they must NOT start with
      ``tempfile.gettempdir()`` (or a normalised fixture prefix in
      ``temp_roots``): the #87 defect was exactly an stl/views path
      under the tempdir the worker tears down.

    ``render_artifact_dir=None`` (persistence disabled/failed) is a valid
    production shape — the fields may then be None/empty.
    """
    from d33d.render_worker import ERROR_CLASSES, RenderResult

    if not isinstance(result, RenderResult):
        raise SeamError(
            "B",
            f"payload is {type(result).__name__}, expected RenderResult "
            "(the seam-B payload is the result.json deserialiser's output)",
        )
    declared = declared_field_names(RenderResult)
    for name in declared:
        if not hasattr(result, name):
            raise SeamError("B", f"declared RenderResult field {name!r} missing")
    if not isinstance(result.ok, bool):
        raise SeamError("B", f"ok is {type(result.ok).__name__}, expected bool")
    for name in ("exit_code", "duration_ms"):
        value = getattr(result, name)
        if not isinstance(value, int) or isinstance(value, bool):
            raise SeamError("B", f"{name} is {type(value).__name__}, expected int")
    if result.error_class not in ERROR_CLASSES:
        raise SeamError(
            "B",
            f"error_class {result.error_class!r} not in the closed 7-class enum",
        )
    if not isinstance(result.stderr, str):
        raise SeamError("B", f"stderr is {type(result.stderr).__name__}, expected str")
    for name in ("stl", "csg", "render_artifact_dir"):
        value = getattr(result, name)
        if value is not None and not isinstance(value, str):
            raise SeamError(
                "B", f"{name} is {type(value).__name__}, expected str or None"
            )
    if not isinstance(result.views, tuple) or not all(isinstance(v, str) for v in result.views):
        raise SeamError("B", "views must be a tuple of str")
    # Durability: when the durable artifact dir is set, stl/views must be
    # re-pointed OFF the tempdir (the #87 invariant).
    if result.render_artifact_dir is not None:
        candidates = [p for p in [result.stl, *result.views] if p is not None]
        for p in candidates:
            if _is_tempdir_path(p, extra_roots=temp_roots):
                raise SeamError(
                    "B",
                    f"{p!r} is under a tempdir root while render_artifact_dir is "
                    "set — the #87 shape (a path the worker's tempdir teardown "
                    "deletes before the loop reads it)",
                )
    return result


# ---------------------------------------------------------------------------
# SEAM C — loop -> SSE adapter: IterationRecord (best)
# ---------------------------------------------------------------------------

#: The attributes ``d33d.design_loop_events`` and
#: ``d33d.versions_routes.finalize`` read off the ``best`` candidate —
#: the seam-C reader surface (issue #93: the adapter once duck-typed
#: ``getattr(best, "params")`` against a field the dataclass did not
#: declare; every reader attribute must be a declared field).
BEST_READER_ATTRIBUTES: tuple[str, ...] = (
    "render",  # _artifact_bytes_from_path (design_loop_events)
    "score",  # the bbox_abstained propagation (design_loop_events)
    "scad_source",  # the token frame (design_loop_events)
    "params",  # _resolve_version_create + versions_routes.finalize
)


def validate_iteration_record_seam_c(record: Any) -> Any:
    """Assert an ``IterationRecord`` (the ``best`` the SSE adapter reads)
    carries every attribute its readers use as a DECLARED field — the
    structural form of the #93 guard (every reader attribute must be
    declared, so a rename/deletion fails LOUDLY here instead of silently
    yielding ``None`` at frame time). Returns the record unchanged.
    """
    from d33d.design_loop import IterationRecord

    if not isinstance(record, IterationRecord):
        raise SeamError(
            "C",
            f"payload is {type(record).__name__}, expected IterationRecord "
            "(the seam-C payload is DesignResult.best)",
        )
    declared = declared_field_names(IterationRecord)
    for name in BEST_READER_ATTRIBUTES:
        if name not in declared:
            raise SeamError(
                "C",
                f"SEAM C reader reads best.{name!r}, which is not a declared "
                f"IterationRecord field (declared: {sorted(declared)}) — the "
                "issue #93 shape (a dead read that silently yields None)",
            )
        if not hasattr(record, name):
            raise SeamError("C", f"record is missing attribute {name!r}")
    if not isinstance(record.iteration, int):
        raise SeamError("C", f"iteration is {type(record.iteration).__name__}, expected int")
    if not isinstance(record.scad_source, str):
        raise SeamError("C", f"scad_source is {type(record.scad_source).__name__}, expected str")
    validate_render_result_seam_b(record.render)
    score = record.score
    if not hasattr(score, "bits") or not hasattr(score, "bbox_abstained"):
        raise SeamError("C", f"score is {type(score).__name__}, expected Score")
    if not isinstance(record.params, dict):
        raise SeamError("C", f"params is {type(record.params).__name__}, expected dict")
    return record
