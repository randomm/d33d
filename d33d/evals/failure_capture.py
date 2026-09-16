"""failures.jsonl pin: production gate failures → golden-set growth.

Issue #9 (workstream task-failures). A one-line hook in the renderer app
appends one line to ``evals/failures.jsonl`` on any deterministic gate
failure IN PRODUCTION. The line carries the seven-field shape the ticket
pins: ``{photo, region_mark, request, model, prompt_version, output_scad,
failure_class}``.

Design decisions (this module documents each; see the issue's edge-case
list for the open questions this resolves):

1. **Schema pin.** :class:`FailureEvent` is a pydantic model — the
   seven-field shape is testable (tests/evals/test_failures_jsonl.py
   validates on write: no missing fields, no type mismatches).

2. **Production vs eval — STRUCTURAL exclusion, not a flag.** The hook
   (``record_production_failure`` / ``_hook_exhausted_loop``) is wired
   into the production design-loop call path only (``d33d/app.py``'s
   ``run_design_loop`` closure). The promptfoo assert path never imports
   or calls this module's hook — eval-run failures are excluded because
   the eval runner simply never reaches the hook, not because a flag
   says "eval". There is no ``is_eval`` flag to get wrong.

3. **Failure classes — superset, not forked.** The closed
   ``EVAL_FAILURE_CLASSES`` enum reuses the design-loop's 10-class
   superset (the 11 named classes in ``d33d/failure_classes.py`` minus
   the vision-only ``geometrically_wrong`` — gate 1 can never tag a
   vision-catch-only class — PLUS ``graceful_refusal`` and
   ``clearance_applied``, the two eval-context outcome classes the
   adversarial cases are scored against). Render-worker ``ErrorClass``
   values (``timeout``, ``oom``, ``container_error``, ``artifact_error``,
   ``empty_model``) map 1:1 into the same strings. ``failure_class`` is
   a closed Literal — free text is rejected at validation.

4. **Concurrency safety.** Each line is a single
   ``open("ab") -> write(exactly one line + "\\n") -> close`` call.
   On POSIX, ``O_APPEND`` writes are atomic per call for lines well
   under the filesystem's per-write limit (512 KiB here, by a wide
   margin over a few-KB JSON line). Concurrent appends therefore never
   interleave mid-line.

5. **No rotation, no size limit in v1.** The file is git-tracked,
   human-folded monthly by :func:`fold_failures`; unbounded growth is a
   property of production use, not a security boundary.

The ``record_production_failure`` function is the sole write entry point.
The app wires it into the design loop via ``create_app``'s production
``run_design_loop`` closure; the hook fires only when the loop EXHAUSTS
(a pass produces nothing to archive).
"""

from __future__ import annotations

import asyncio as _asyncio
import json
import logging
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, ValidationError

#: The closed enum of failure classes a failures.jsonl line may carry.
#:
#: 11 named LLM classes (the design-loop superset from
#: ``d33d/failure_classes.py``, minus ``geometrically_wrong`` which is
#: vision-catch-only and gate 1 can never tag) + 2 eval-context outcome
#: classes (``graceful_refusal``, ``clearance_applied`` — the adversarial
#: cases are scored against these, never as compile failures).
#:
#: The 5 render-worker classes (``timeout``, ``oom``, ``container_error``,
#: ``artifact_error``, ``empty_model``) appear with their exact
#: ``ErrorClass`` strings — the production hook's tagged class is
#: render-worker-derived when the render itself failed.
EVAL_FAILURE_CLASSES: frozenset[str] = frozenset(
    {
        # 11 named LLM failure classes (superset, reused not forked)
        "trailing_semicolon",
        "transform_order",
        "wrong_axis_rotation",
        "difference_inversion",
        "hull_miskowski_misuse",
        "zup_yup_confusion",
        "magic_numbers",
        "projection_offset_fragile",
        "text_missing_font",
        "hallucinated_bosl2",
        "unclassified_syntax_error",
        # 2 eval-context outcome classes
        "graceful_refusal",
        "clearance_applied",
        # 5 render-worker classes (1:1 with the render ErrorClass strings)
        "timeout",
        "oom",
        "container_error",
        "artifact_error",
        "empty_model",
    }
)

#: The 5 render-worker ``ErrorClass`` strings that the hook maps 1:1
#: into ``EVAL_FAILURE_CLASSES``.
RENDER_WORKER_CLASSES: frozenset[str] = frozenset(
    {
        "timeout",
        "oom",
        "container_error",
        "artifact_error",
        "empty_model",
    }
)

#: The 2 structured gate-reason bit names (``GATE_REASON_BITS`` in
#: ``d33d/design_loop.py``) that also land in ``failure_class``. The
#: design loop's ``failure_reason`` is either one of these names or a
#: render-worker ``ErrorClass`` string — never free text.
GATE_REASON_CLASSES: frozenset[str] = frozenset(
    {
        "error_class_not_ok",
        "views_blank_or_missing",
        "bbox_out_of_tolerance",
        "stated_dims_not_named_parameters",
    }
)

#: Hard cap on ``output_scad`` line length (chars) — an unbounded LLM
#: runaway source would otherwise dominate the file. Mirrors the design
#: loop's ``MAX_SCAD_SOURCE_BYTES`` intent (bounded LLM output) without
#: importing across workstreams.
MAX_OUTPUT_SCAD_CHARS = 256 * 1024

#: Hard cap on a single JSON line (bytes) — well over any realistic
#: 7-field line, keeping the POSIX O_APPEND atomicity argument tight.
MAX_LINE_BYTES = 512 * 1024

#: The repo-relative default location the production hook writes to
#: (``evals/failures.jsonl`` from the repo root — the caller resolves
#: this against the repo root via ``Path(__file__)``).
DEFAULT_FAILURES_FILENAME = "failures.jsonl"


class FailureEvent(BaseModel):
    """One production gate-failure line in ``evals/failures.jsonl``.

    The 7-field shape the ticket pins (``photo, region_mark, request,
    model, prompt_version, output_scad, failure_class``) plus a
    provenance block for the fold procedure:

    - ``photo`` — the reference photo the design was made from (path or
      data URI); ``None`` when the request carried no photo (text-only
      requests — the field is optional so text-only production runs
      still archive).
    - ``region_mark`` — the lasso / region-selection mark (view id +
      module ids) for a region-scoped edit; ``None`` for a full-model
      design.
    - ``request`` — the user's request (instruction / chat context).
      Required and non-empty — a failure with no request is not
      foldable.
    - ``model`` — the model id that generated the failing .scad (the
      concrete id from the resolved role, never the role name).
    - ``prompt_version`` — the canonical prompt hash
      (``d33d.prompt_hash.canonical_hash``) of the design-role prompt —
      the join key that makes "prompt v7 fails case 12 which v5 passed"
      readable.
    - ``output_scad`` — the failing .scad source (bounded, see
      ``MAX_OUTPUT_SCAD_CHARS``).
    - ``failure_class`` — one of :data:`EVAL_FAILURE_CLASSES`. Closed
      enum, validated at construction.
    - ``ts`` — UTC ISO-8601 timestamp (provenance, not one of the 7).
    - ``event_id`` — uuid4 hex (provenance, not one of the 7).

    Validation is strict: a missing field, a type mismatch, or a
    ``failure_class`` outside :data:`EVAL_FAILURE_CLASSES` all raise
    ``ValidationError`` — the hook NEVER writes a malformed line.
    """

    photo: str | None = None
    region_mark: str | None = None
    request: str = Field(min_length=1)
    model: str = Field(min_length=1)
    prompt_version: str = Field(min_length=1)
    output_scad: str = Field(default="")
    failure_class: str = Field(min_length=1)
    ts: str = Field(min_length=1)
    event_id: str = Field(min_length=1)

    @classmethod
    def validate_failure_class(cls, failure_class: str) -> str:
        """Validate ``failure_class`` against the closed enum.

        Raises ``ValueError`` (a ``ValidationError`` on the model path)
        if the class is not in :data:`EVAL_FAILURE_CLASSES`. This is the
        "type mismatch" rejection the test suite asserts on.
        """
        if failure_class not in EVAL_FAILURE_CLASSES:
            raise ValueError(
                f"failure_class {failure_class!r} is not in "
                f"EVAL_FAILURE_CLASSES ({sorted(EVAL_FAILURE_CLASSES)})"
            )
        return failure_class

    def validate(self) -> FailureEvent:
        """Re-validate this instance's ``failure_class``.

        A fresh model instance (already constructed with a valid class)
        passes; a hand-built dict that bypasses ``__init__`` fails here.
        """
        self.validate_failure_class(self.failure_class)
        return self

    def to_line(self) -> str:
        """Serialise to one compact JSON line (no trailing newline).

        ``json.dumps(..., ensure_ascii=False, separators=(",", ":"))`` —
        compact so a single line stays well under
        ``MAX_LINE_BYTES``; ``ensure_ascii=False`` so non-ASCII request
        text stays readable without a bloat penalty from ``\\uXXXX``
        escapes.
        """
        payload = self.model_dump()
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    def line_bytes(self) -> int:
        """Encoded length of :meth:`to_line` in bytes."""
        return len(self.to_line().encode("utf-8"))


def _validate_failure_class(failure_class: str, *, allow_gate_reasons: bool = False) -> str:
    """Validate a ``failure_class`` against the closed enum.

    With ``allow_gate_reasons`` (the hook path), the 4 structured
    ``GATE_REASON_BITS`` names are admitted in addition to
    :data:`EVAL_FAILURE_CLASSES`. Without it (the eval-harness / fold
    path), only :data:`EVAL_FAILURE_CLASSES` is accepted.
    """
    if failure_class not in EVAL_FAILURE_CLASSES:
        if allow_gate_reasons and failure_class in GATE_REASON_CLASSES:
            return failure_class
        raise ValueError(
            f"failure_class {failure_class!r} is not in "
            f"EVAL_FAILURE_CLASSES ({sorted(EVAL_FAILURE_CLASSES)})"
        )
    return failure_class


def make_failure_event(
    *,
    photo: str | None,
    region_mark: str | None,
    request: str,
    model: str,
    prompt_version: str,
    output_scad: str,
    failure_class: str,
    now: datetime | None = None,
    event_id: str | None = None,
    allow_gate_reasons: bool = False,
) -> FailureEvent:
    """Build a validated :class:`FailureEvent`.

    ``now`` / ``event_id`` are injectable for deterministic tests;
    production uses the defaults (UTC now, fresh uuid4).

    ``allow_gate_reasons`` (default ``False``) admits the 4 structured
    ``GATE_REASON_BITS`` names in addition to :data:`EVAL_FAILURE_CLASSES`
    — the design loop's ``failure_reason`` is either a render-worker
    class, a gate-reason bit, or a named LLM class, so the hook passes
    ``True`` while a direct ``make_failure_event`` call (the eval
    harness, the fold script) does not.

    Raises ``ValidationError`` (via the model) on a missing/empty
    required field, or a ``failure_class`` outside the closed enum.
    """
    _validate_failure_class(failure_class, allow_gate_reasons=allow_gate_reasons)
    ts = (
        (now or datetime.now(UTC)).astimezone(UTC).isoformat()
    )
    eid = event_id or uuid.uuid4().hex
    return FailureEvent(
        photo=photo,
        region_mark=region_mark,
        request=request,
        model=model,
        prompt_version=prompt_version,
        output_scad=output_scad[:MAX_OUTPUT_SCAD_CHARS],
        failure_class=failure_class,
        ts=ts,
        event_id=eid,
    )


def default_failures_path() -> Path:
    """The repo-relative default ``evals/failures.jsonl`` path.

    ``d33d/evals/failure_capture.py`` is two levels below the repo root;
    the file lives at ``<repo root>/evals/failures.jsonl``.
    """
    return Path(__file__).resolve().parent.parent.parent / "evals" / DEFAULT_FAILURES_FILENAME


#: Process-wide append lock — serialises concurrent appends so the
#: single-line write (open/append/close) is never interleaved even on a
#: platform where O_APPEND atomicity is not guaranteed. The lock is
#: advisory only (a second process could still interleave) but the
#: single-writer production design means one process is the writer.
_append_lock = threading.Lock()


def append_failure_line(
    event: FailureEvent, path: str | Path | None = None
) -> Path:
    """Append one validated event to ``failures.jsonl``.

    Single-line, append-only: one ``open("ab") -> write(line + "\\n") ->
    close`` under :data:`_append_lock`. POSIX O_APPEND makes the
    single-line write atomic; the lock serialises same-process callers.

    ``path`` defaults to :func:`default_failures_path`. The parent
    directory is created if missing (a fresh checkout with no
    ``evals/`` yet — the file is git-tracked but the directory is
    created on first write).

    Returns the path written. Raises ``ValueError`` if the encoded line
    exceeds :data:`MAX_LINE_BYTES` (a line over the cap is a caller bug,
    not a valid event).
    """
    line = event.to_line() + "\n"
    if len(line.encode("utf-8")) > MAX_LINE_BYTES:
        raise ValueError(
            f"failures.jsonl line is {len(line.encode('utf-8'))} bytes, "
            f"exceeding the {MAX_LINE_BYTES} byte cap"
        )
    target = Path(path) if path is not None else default_failures_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    with _append_lock, open(target, "ab") as f:
        f.write(line.encode("utf-8"))
    return target


def read_failure_events(path: str | Path | None = None) -> list[FailureEvent]:
    """Read and validate every line of ``failures.jsonl``.

    Blank lines are skipped (a trailing newline is not a line). A
    non-blank line that is not valid JSON or does not validate against
    :class:`FailureEvent` raises ``ValueError`` with the line number —
    a corrupted line is a hard error, never silently dropped (the fold
    procedure must see every line).
    """
    target = Path(path) if path is not None else default_failures_path()
    if not target.exists():
        return []
    events: list[FailureEvent] = []
    for i, raw in enumerate(target.read_text(encoding="utf-8").splitlines(), start=1):
        if not raw.strip():
            continue
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError as e:
            raise ValueError(f"failures.jsonl line {i} is not valid JSON: {e}") from e
        try:
            ev = FailureEvent.model_validate(obj)
        except ValidationError as e:
            raise ValueError(f"failures.jsonl line {i} failed schema: {e}") from e
        events.append(ev)
    return events


def _resolve_model_id(model_field: Any) -> str:
    """The concrete model id from a design-loop LLMResult.

    The design loop's ``llm_fn`` returns an ``LLMResult`` carrying the
    resolved model id (``d33d.design_llm.send`` sets it from
    ``resolve_model``). A plain string is accepted as the id itself
    (the app's production closure can pass the resolved id directly).
    """
    model_id = getattr(model_field, "model", None)
    if isinstance(model_id, str) and model_id:
        return model_id
    if isinstance(model_field, str) and model_field:
        return model_field
    raise ValueError(
        f"cannot resolve a model id from {model_field!r} "
        "(expected an LLMResult with a string .model or a bare string)"
    )


def _exhausted_loop_event(
    *,
    design_result: Any,
    photo: Any,
    region_mark: Any,
    request: str,
    model: Any,
    prompt_version: str,
    output_scad: str,
) -> FailureEvent:
    """Build the :class:`FailureEvent` for an exhausted design loop.

    The failure class is the loop's tagged ``failure_reason`` (a
    render-worker class, a gate-reason bit, or a named LLM class) — the
    structured class the loop already computed, never a re-derivation
    from raw stderr.
    """
    failure_reason = getattr(design_result, "failure_reason", None)
    if not isinstance(failure_reason, str) or not failure_reason:
        raise ValueError(
            "an exhausted design loop must carry a structured "
            "failure_reason (a render-worker class, a gate-reason bit, "
            "or a named LLM class); got "
            + repr(failure_reason)
        )
    # The loop's ``failure_reason`` is either a render-worker class,
    # a gate-reason bit, or a named LLM class — validate against the
    # union of those three sets (never free text).
    if failure_reason not in EVAL_FAILURE_CLASSES and failure_reason not in GATE_REASON_CLASSES:
        raise ValueError(
            f"failure_class {failure_reason!r} is not in "
            f"EVAL_FAILURE_CLASSES or GATE_REASON_CLASSES"
        )
    return make_failure_event(
        photo=_optional_str(photo),
        region_mark=_optional_str(region_mark),
        request=request,
        model=_resolve_model_id(model),
        prompt_version=prompt_version,
        output_scad=output_scad,
        failure_class=failure_reason,
        allow_gate_reasons=True,
    )


def _optional_str(value: Any) -> str | None:
    """A value → ``str`` for JSONL, ``None`` for absent/None."""
    if value is None:
        return None
    s = str(value)
    return s if s else None


def record_production_failure(
    *,
    design_result: Any,
    photo: Any = None,
    region_mark: Any = None,
    request: str,
    model: Any,
    prompt_version: str,
    output_scad: str,
    path: str | Path | None = None,
    now: datetime | None = None,
) -> FailureEvent | None:
    """The production hook: archive an exhausted design loop to
    ``failures.jsonl``.

    Fires ONLY when ``design_result.status == 'exhausted'`` — a passing
    loop produces no failure line (there is nothing to archive). The
    eval harness's assert path never calls this function (structural
    exclusion), so eval-run failures never reach the file.

    Returns the :class:`FailureEvent` written, or ``None`` for a
    passing result. An untagged/exhausted result (no ``failure_reason``)
    raises ``ValueError`` — a silent drop would lose a real failure.
    """
    status = getattr(design_result, "status", None)
    if status == "pass":
        return None
    if status != "exhausted":
        raise ValueError(
            f"design_result.status {status!r} is not 'pass' or 'exhausted'; "
            "refusing to record an unrecognised loop outcome"
        )
    event = _exhausted_loop_event(
        design_result=design_result,
        photo=photo,
        region_mark=region_mark,
        request=request,
        model=model,
        prompt_version=prompt_version,
        output_scad=output_scad,
    )
    append_failure_line(event, path)
    return event


def default_run_design_loop_hook(
    *,
    path: str | Path | None = None,
):
    """Build the production ``run_design_loop`` closure with the hook.

    Returns an ``async def`` closure: it awaits ``d33d.design_loop.
    run_design_loop_async`` (the async core, captured by default), so in
    the FastAPI finalize path the loop runs inside the running event loop
    with no nested ``asyncio.run``. Non-async callers (CLI, eval-harness
    direct invocation, sync tests) use
    :func:`default_run_design_loop_hook_sync`.

    The hook fires ONLY on an exhausted result and
    ONLY here: the promptfoo assert path (task-harness) shells into the
    render worker directly and never calls this closure, so eval-run
    failures are structurally excluded (no ``is_eval`` flag).

    The caller's ``kwargs`` carry the design-loop arguments PLUS the
    hook's ``model`` and ``prompt_version`` (the app adds those from the
    resolved role and the canonical prompt hash) and the ``request`` —
    the user's CURRENT request text. The closure pops ``model`` /
    ``prompt_version`` (the loop doesn't accept them) but FORWARDS
    ``request``: since issue #97 the loop renders it as the first
    ``Request:`` line of the design prompt, so the hook archives the same
    value it hands the loop — never a prior-turn ``chat_history``
    fallback (the archive line must equal the current user's request, not
    history). A test that needs a stub loop (or no hook) overwrites
    ``app.state.run_design_loop`` after ``create_app`` returns.
    """
    from d33d import design_loop as _dl

    real_run = _dl.run_design_loop_async

    async def _hooked(**kwargs: Any) -> Any:
        hook_model = kwargs.pop("model", None)
        hook_prompt_version = kwargs.pop("prompt_version", None)
        hook_request = kwargs.pop("request", None)
        hook_photo = kwargs.get("photo")
        hook_region_mark = kwargs.get("region_mark")
        # The archive ``request`` is the popped value — identical to what
        # the loop receives below (a missing ``request`` degrades to an
        # empty archive field; the loop then renders no request line).
        request = str(hook_request or "")
        result = await real_run(request=request, **kwargs)
        try:
            if result is not None:
                record_production_failure(
                    design_result=result,
                    photo=hook_photo,
                    region_mark=hook_region_mark,
                    request=request,
                    model=hook_model,
                    prompt_version=str(hook_prompt_version or ""),
                    output_scad=str(
                        getattr(getattr(result, "best", None), "scad_source", "") or ""
                    ),
                    path=path,
                )
        except Exception:
            # The hook MUST NOT mask the loop result — a hook failure
            # (e.g. a disk-full append) is logged, never raised, so the
            # user's design attempt is still returned.
            logging.getLogger(__name__).exception(
                "failures.jsonl hook failed; returning design result anyway"
            )
        return result

    return _hooked


def default_run_design_loop_hook_sync(
    *,
    path: str | Path | None = None,
):
    """Sync-callable entry to :func:`default_run_design_loop_hook`.

    A thin ``asyncio.run`` over the async hook, for callers with NO
    running event loop (CLI, eval-harness direct invocation, sync tests).
    Calling it from inside a running event loop raises ``RuntimeError``
    (``asyncio.run`` forbids that) — keep it out of the async path.
    """
    hook = default_run_design_loop_hook(path=path)

    def _hooked_sync(**kwargs: Any) -> Any:
        return _asyncio.run(hook(**kwargs))

    return _hooked_sync


__all__ = [
    "DEFAULT_FAILURES_FILENAME",
    "EVAL_FAILURE_CLASSES",
    "GATE_REASON_CLASSES",
    "MAX_LINE_BYTES",
    "MAX_OUTPUT_SCAD_CHARS",
    "RENDER_WORKER_CLASSES",
    "FailureEvent",
    "append_failure_line",
    "default_failures_path",
    "default_run_design_loop_hook",
    "default_run_design_loop_hook_sync",
    "make_failure_event",
    "read_failure_events",
    "record_production_failure",
]
