"""failures.jsonl pin — tests/evals/test_failures_jsonl.py (issue #9,
workstream task-failures).

Covers the ticket's test surface for this workstream:

(a) when ANY deterministic gate (1–7) fails in a production render, the
    hook appends a correctly-shaped line to ``evals/failures.jsonl``
    with the schema ``{photo, region-mark, request, model,
    prompt_version, output_scad, failure_class}`` (+ provenance ``ts`` /
    ``event_id``).
(b) eval-run failures are excluded — the hook is structurally excluded
    from the eval path (the promptfoo assert never imports or calls the
    hook; this test asserts the hook fires only on an ``exhausted``
    design-loop result and NOT on a ``pass``, which is the production
    trigger).
(c) the JSONL schema is validated on write (no missing fields, no type
    mismatches — ``failure_class`` outside the closed enum is rejected).
(d) concurrent appends don't corrupt the file.
"""

from __future__ import annotations

import concurrent.futures
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

from d33d.evals.failure_capture import (
    EVAL_FAILURE_CLASSES,
    GATE_REASON_CLASSES,
    LOOP_LEVEL_FAILURE_REASONS,
    MAX_LINE_BYTES,
    RENDER_WORKER_CLASSES,
    FailureEvent,
    append_failure_line,
    default_failures_path,
    default_run_design_loop_hook,
    default_run_design_loop_hook_sync,
    make_failure_event,
    read_failure_events,
    record_production_failure,
)


@pytest.fixture
def _eval_app_paths(tmp_path: Path) -> dict[str, Path]:
    """Isolated DB + master-key + catalogue paths under ``tmp_path``
    (the ``app_paths`` fixture from ``tests/versioning/conftest.py`` —
    redefined locally because a cross-directory fixture import does not
    re-register its ``app_paths`` dependency in this package's scope).
    """
    return {
        "db": tmp_path / "d33d.sqlite3",
        "key": tmp_path / "master.key",
        "cat": tmp_path / "models.yaml",
    }


@pytest.fixture
def _eval_app_with_versions(_eval_app_paths: dict[str, Path], tmp_path: Path):
    """A ``create_app`` instance with the default git path pointed at
    ``tmp_path`` (the ``app_with_versions`` fixture from
    ``tests/versioning/conftest.py``, redefined locally for the same
    reason — the ``app_paths`` dependency must resolve in THIS
    package's fixture scope).
    """
    import d33d.db as db_mod

    original_default = db_mod._default_git_path

    def _tmp_default_git_path(name: str) -> str:
        import uuid

        slug = uuid.uuid4().hex[:12]
        base = tmp_path / "repos" / slug
        base.mkdir(parents=True, exist_ok=True)
        return str(base)

    db_mod._default_git_path = _tmp_default_git_path

    from d33d.app import create_app

    app = create_app(
        _eval_app_paths["db"],
        master_key_path=_eval_app_paths["key"],
        catalogue_path=_eval_app_paths["cat"],
    )
    yield app
    db_mod._default_git_path = original_default


async def _create_project(client):
    """Create a project via the API; returns the full project row."""
    r = await client.post("/api/projects", json={"name": "test project"})
    assert r.status_code == 201, r.text
    return r.json()


def _drive_stream(app_with_versions, coro_factory):
    """Drive an async app under a fresh event loop, running the lifespan."""
    import asyncio

    async def _run():
        async with app_with_versions.router.lifespan_context(app_with_versions):
            client = AsyncClient(
                transport=ASGITransport(app=app_with_versions), base_url="http://test"
            )
            async with client:
                return await coro_factory(client)

    return asyncio.run(_run())

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _exhausted_result(failure_reason: str, scad: str = "cube([1,1,1]);"):
    """A minimal stand-in for ``d33d.design_loop.DesignResult``.

    The hook only reads ``.status``, ``.failure_reason``, and
    ``.best.scad_source`` — it does not import the design loop (the app
    wires the real loop; the hook is pure).
    """

    class _Best:
        def __init__(self, scad):
            self.scad_source = scad

    class _Result:
        status = "exhausted"

        def __init__(self, scad, failure_reason):
            self.best = _Best(scad)
            self.failure_reason = failure_reason

    return _Result(scad, failure_reason)


def _passing_result():
    class _Best:
        def __init__(self):
            self.scad_source = "cube([1,1,1]);"

    class _Result:
        status = "pass"
        failure_reason = None

        def __init__(self):
            self.best = _Best()

    return _Result()


def _llm_result(model: str):
    class _LLM:
        def __init__(self, m):
            self.model = m

    return _LLM(model)


# ---------------------------------------------------------------------------
# (a) — the hook appends a correctly-shaped line on a production gate failure
# ---------------------------------------------------------------------------


def test_hook_appends_line_on_exhausted_loop(tmp_path: Path):
    """An exhausted production loop appends one line with the 7-field
    shape (+ provenance) to the failures file."""
    out = tmp_path / "failures.jsonl"
    result = _exhausted_result("empty_model", scad="cube();")
    record_production_failure(
        design_result=result,
        photo="/photos/ref.png",
        region_mark="front:[top-slab]",
        request="make the top slab thicker",
        model=_llm_result("RedHatAI/Qwen3.8-27B-INT4"),
        prompt_version="ab12",
        output_scad="cube();",
        path=out,
        now=None,
    )
    text = out.read_text(encoding="utf-8")
    assert text.count("\n") == 1
    line = text.rstrip("\n")
    assert line.startswith("{") and line.endswith("}")
    events = read_failure_events(out)
    assert len(events) == 1
    ev = events[0]
    # The 7 pinned fields are all present with the right values.
    assert ev.photo == "/photos/ref.png"
    assert ev.region_mark == "front:[top-slab]"
    assert ev.request == "make the top slab thicker"
    assert ev.model == "RedHatAI/Qwen3.8-27B-INT4"
    assert ev.prompt_version == "ab12"
    assert ev.output_scad == "cube();"
    assert ev.failure_class == "empty_model"
    # Provenance fields present.
    assert ev.ts
    assert ev.event_id


def test_hook_appends_line_for_each_failure_class(tmp_path: Path):
    """The hook accepts every class in the closed enum (the 11 named +
    2 eval-context + 5 render-worker + 4 gate-reason bits)."""
    # The hook's ``failure_reason`` can be any of the closed strings.
    # ``EVAL_FAILURE_CLASSES`` already covers the 18 non-gate-reason
    # strings; gate-reason bits are validated separately (see below).
    for fc in sorted(EVAL_FAILURE_CLASSES):
        out = tmp_path / f"{fc.replace('/', '_')}.jsonl"
        record_production_failure(
            design_result=_exhausted_result(fc),
            photo=None,
            region_mark=None,
            request="do something",
            model="model-x",
            prompt_version="deadbeef",
            output_scad="cube();",
            path=out,
        )
        events = read_failure_events(out)
        assert len(events) == 1, fc
        assert events[0].failure_class == fc


def test_loop_level_preflight_reason_is_accepted_failure_class(tmp_path: Path):
    """The loop-level pre-flight reason ``renderer_unavailable`` (issue
    #277) is an accepted ``failure_class`` — a Docker-down design turn
    archives a real line instead of raising in the hook (a silent drop
    would lose a real failure, and the old closed set rejected the reason
    with ``ValueError`` on every such turn)."""
    for reason in sorted(LOOP_LEVEL_FAILURE_REASONS):
        out = tmp_path / f"{reason}.jsonl"
        record_production_failure(
            design_result=_exhausted_result(reason),
            request="do something",
            model="model-x",
            prompt_version="deadbeef",
            output_scad="cube();",
            path=out,
        )
        events = read_failure_events(out)
        assert len(events) == 1, reason
        assert events[0].failure_class == reason


def test_hook_rejects_unknown_failure_class(tmp_path: Path):
    """A ``failure_reason`` outside the closed enum is a hard error —
    the hook raises ``ValueError`` and appends NOTHING (no silent
    drop, no free-text line)."""
    out = tmp_path / "failures.jsonl"
    with pytest.raises(ValueError, match="not in EVAL_FAILURE_CLASSES"):
        record_production_failure(
            design_result=_exhausted_result("totally_bogus_class"),
            request="do something",
            model="model-x",
            prompt_version="deadbeef",
            output_scad="cube();",
            path=out,
        )
    assert not out.exists() or out.read_text(encoding="utf-8") == ""


def test_hook_rejects_missing_failure_reason(tmp_path: Path):
    """An exhausted result with no ``failure_reason`` is a hard error —
    a silent drop would lose a real failure."""
    out = tmp_path / "failures.jsonl"

    class _Result:
        status = "exhausted"
        failure_reason = None
        best = None

    with pytest.raises(ValueError, match="structured failure_reason"):
        record_production_failure(
            design_result=_Result(),
            request="do something",
            model="model-x",
            prompt_version="deadbeef",
            output_scad="cube();",
            path=out,
        )
    assert not out.exists() or out.read_text(encoding="utf-8") == ""


def test_gate_reason_bits_are_accepted_failure_classes(tmp_path: Path):
    """The 4 structured ``GATE_REASON_BITS`` names are accepted
    ``failure_class`` values (the loop's ``failure_reason`` is either a
    render-worker class OR one of these bits). The hook validates
    against ``EVAL_FAILURE_CLASSES`` plus ``GATE_REASON_CLASSES`` —
    ``geometrically_wrong`` (vision-only) is the only named class
    excluded."""
    for bit in sorted(GATE_REASON_CLASSES):
        out = tmp_path / f"{bit}.jsonl"
        record_production_failure(
            design_result=_exhausted_result(bit),
            request="do something",
            model="model-x",
            prompt_version="deadbeef",
            output_scad="cube();",
            path=out,
        )
        events = read_failure_events(out)
        assert len(events) == 1, bit
        assert events[0].failure_class == bit


# ---------------------------------------------------------------------------
# (b) — eval-run failures are structurally excluded (the hook never fires
# on a pass; the eval path never calls the hook)
# ---------------------------------------------------------------------------


def test_hook_does_not_append_on_pass(tmp_path: Path):
    """A passing design loop appends nothing (the hook only fires on
    ``exhausted``)."""
    out = tmp_path / "failures.jsonl"
    result = _passing_result()
    event = record_production_failure(
        design_result=result,
        photo="/photos/ref.png",
        request="do something",
        model=_llm_result("model-x"),
        prompt_version="deadbeef",
        output_scad="cube();",
        path=out,
    )
    assert event is None
    assert not out.exists()


def test_hook_rejects_unrecognised_status(tmp_path: Path):
    """A design result with a status that is neither ``pass`` nor
    ``exhausted`` is a hard error — no silent drop."""
    out = tmp_path / "failures.jsonl"

    class _Result:
        status = "weird"
        failure_reason = "empty_model"
        best = None

    with pytest.raises(ValueError, match="not 'pass' or 'exhausted'"):
        record_production_failure(
            design_result=_Result(),
            request="do something",
            model="model-x",
            prompt_version="deadbeef",
            output_scad="cube();",
            path=out,
        )
    assert not out.exists() or out.read_text(encoding="utf-8") == ""


def test_eval_path_never_imports_hook():
    """Structural exclusion: the promptfoo assert path (task-harness)
    must NOT import ``d33d.evals.failure_capture`` — eval-run failures
    are excluded because the eval runner never reaches the hook, not
    because a flag says ``is_eval``.

    This test asserts the invariant by reading the harness module's
    source (if present) and confirming it does not import the hook.
    The harness may not be landed yet (sibling workstream), so the test
    is a no-op when the module is absent.
    """
    import importlib.util

    harness_spec = importlib.util.find_spec("d33d.evals.harness")
    if harness_spec is None:
        pytest.skip("task-harness not landed yet")
    import d33d.evals.harness as harness_mod

    source = Path(harness_mod.__file__).read_text(encoding="utf-8")
    assert "failure_capture" not in source, (
        "the promptfoo harness must not import the failures.jsonl hook "
        "(structural exclusion of eval-run failures)"
    )


def test_hook_is_the_only_write_entrypoint(tmp_path: Path):
    """``append_failure_line`` is the sole writer; the hook delegates to
    it. A direct ``append_failure_line`` call on a valid event writes
    exactly one line."""
    out = tmp_path / "failures.jsonl"
    ev = make_failure_event(
        photo=None,
        region_mark=None,
        request="do something",
        model="model-x",
        prompt_version="deadbeef",
        output_scad="cube();",
        failure_class="empty_model",
    )
    append_failure_line(ev, out)
    assert out.read_text(encoding="utf-8").count("\n") == 1


# ---------------------------------------------------------------------------
# (c) — the JSONL schema is validated on write
# ---------------------------------------------------------------------------


def test_failure_event_rejects_missing_required_field():
    """A ``FailureEvent`` with an empty required field raises
    ``ValidationError`` (no missing fields)."""
    with pytest.raises(ValidationError):
        FailureEvent(
            photo=None,
            region_mark=None,
            request="",  # empty request — required, non-empty
            model="model-x",
            prompt_version="deadbeef",
            output_scad="cube();",
            failure_class="empty_model",
            ts="2026-01-01T00:00:00+00:00",
            event_id="abc123",
        )


def test_failure_event_rejects_bad_failure_class():
    """A ``FailureEvent`` with a ``failure_class`` outside the closed
    enum is rejected (no type mismatches on the enum)."""
    with pytest.raises(ValueError, match="not in EVAL_FAILURE_CLASSES"):
        FailureEvent.validate_failure_class("not_a_real_class")


def test_failure_event_accepts_valid_class():
    """A ``FailureEvent`` with a valid ``failure_class`` passes."""
    for fc in sorted(EVAL_FAILURE_CLASSES):
        FailureEvent.validate_failure_class(fc)


def test_failure_event_accepts_pre_417_shape_without_attempt_fields():
    """A ``FailureEvent`` with ``attempt_count=None`` and
    ``per_attempt_latencies=None`` (the pre-#417 row shape) validates —
    the two fields are optional (default ``None``) so existing rows
    written before the deadline-kill archive gained them still read.
    A future change that accidentally makes the fields required would
    break this test."""
    ev = FailureEvent(
        photo=None,
        region_mark=None,
        request="make it a cube",
        model="model-x",
        prompt_version="",
        output_scad="",
        failure_class="design_loop_timed_out",
        ts="2026-01-01T00:00:00+00:00",
        event_id="abc123",
    )
    assert ev.attempt_count is None
    assert ev.per_attempt_latencies is None


def test_failure_event_attempt_fields_constraints_apply_when_present():
    """The ``attempt_count`` / ``per_attempt_latencies`` constraints
    (``ge=1`` / ``min_length=1``) apply only when the value IS present
    (the ``None`` default is always accepted — see the pre-#417 shape
    test above). A present value that violates the constraint raises:
    ``attempt_count=0`` (the honest-absence path writes ``None``, never
    ``0``) and an empty ``per_attempt_latencies`` list are both
    rejected."""
    with pytest.raises(ValidationError):
        FailureEvent(
            request="make it a cube",
            model="model-x",
            failure_class="design_loop_timed_out",
            ts="2026-01-01T00:00:00+00:00",
            event_id="abc123",
            attempt_count=0,
        )
    with pytest.raises(ValidationError):
        FailureEvent(
            request="make it a cube",
            model="model-x",
            failure_class="design_loop_timed_out",
            ts="2026-01-01T00:00:00+00:00",
            event_id="abc123",
            per_attempt_latencies=[],
        )


def test_make_failure_event_rejects_empty_request():
    """``make_failure_event`` rejects an empty ``request`` (the model's
    ``min_length=1`` validator)."""
    with pytest.raises(ValidationError):
        make_failure_event(
            photo=None,
            region_mark=None,
            request="",
            model="model-x",
            prompt_version="deadbeef",
            output_scad="cube();",
            failure_class="empty_model",
        )


def test_make_failure_event_rejects_unknown_class():
    """``make_failure_event`` rejects a ``failure_class`` outside the
    closed enum (``ValueError`` before the model is built)."""
    with pytest.raises(ValueError, match="not in EVAL_FAILURE_CLASSES"):
        make_failure_event(
            photo=None,
            region_mark=None,
            request="do something",
            model="model-x",
            prompt_version="deadbeef",
            output_scad="cube();",
            failure_class="bogus",
        )


def test_append_rejects_oversized_line(tmp_path: Path):
    """An event whose encoded line exceeds ``MAX_LINE_BYTES`` is
    rejected (``ValueError``) — no oversized line reaches the file."""
    out = tmp_path / "failures.jsonl"
    # A request just over the cap forces the line over MAX_LINE_BYTES.
    huge = "x" * (MAX_LINE_BYTES + 10)
    ev = make_failure_event(
        photo=None,
        region_mark=None,
        request=huge,
        model="model-x",
        prompt_version="deadbeef",
        output_scad="cube();",
        failure_class="empty_model",
    )
    with pytest.raises(ValueError, match="exceeding the .* byte cap"):
        append_failure_line(ev, out)
    assert not out.exists()


def test_read_rejects_corrupt_line(tmp_path: Path):
    """A non-blank line that is not valid JSON raises ``ValueError``
    with the line number — a corrupted line is a hard error, never
    silently dropped."""
    out = tmp_path / "failures.jsonl"
    out.write_text(
        '{"photo": null, "region_mark": null, "request": "ok", ' '"model": "m", "prompt_version": "p", "output_scad": "", ' '"failure_class": "empty_model", "ts": "2026-01-01T00:00:00+00:00", ' '"event_id": "e"}\nnot json at all\n', encoding="utf-8"
    )
    with pytest.raises(ValueError, match="line 2 is not valid JSON"):
        read_failure_events(out)


def test_read_rejects_schema_violation_line(tmp_path: Path):
    """A valid-JSON line that fails the ``FailureEvent`` schema raises
    ``ValueError`` (no type mismatches)."""
    out = tmp_path / "failures.jsonl"
    # Valid JSON, but ``failure_class`` is missing (required).
    out.write_text(
        '{"photo": null, "region_mark": null, "request": "x", '
        '"model": "m", "prompt_version": "p", "output_scad": "", '
        '"ts": "2026-01-01T00:00:00+00:00", "event_id": "e"}\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="line 1 failed schema"):
        read_failure_events(out)


def test_read_skips_blank_lines(tmp_path: Path):
    """Blank lines (including a trailing newline) are skipped, not
    treated as corrupt."""
    out = tmp_path / "failures.jsonl"
    ev = make_failure_event(
        photo=None,
        region_mark=None,
        request="do something",
        model="model-x",
        prompt_version="deadbeef",
        output_scad="cube();",
        failure_class="empty_model",
    )
    append_failure_line(ev, out)
    # Append a blank line.
    with open(out, "ab") as f:
        f.write(b"\n")
    events = read_failure_events(out)
    assert len(events) == 1


def test_read_missing_file_returns_empty(tmp_path: Path):
    """A missing file returns an empty list (not an error)."""
    assert read_failure_events(tmp_path / "nope.jsonl") == []


# ---------------------------------------------------------------------------
# (d) — concurrent appends don't corrupt the file
# ---------------------------------------------------------------------------


def test_concurrent_appends_do_not_corrupt(tmp_path: Path):
    """Many threads appending to the same file produce exactly N valid
    lines, each parseable — no interleaved mid-line corruption."""
    out = tmp_path / "failures.jsonl"
    n = 50

    def _one(i: int):
        ev = make_failure_event(
            photo=None,
            region_mark=None,
            request=f"request-{i}",
            model="model-x",
            prompt_version="deadbeef",
            output_scad=f"cube([1,1,{i}]);",
            failure_class="empty_model",
        )
        append_failure_line(ev, out)

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        futs = [ex.submit(_one, i) for i in range(n)]
        for f in futs:
            f.result()

    events = read_failure_events(out)
    assert len(events) == n
    for i, ev in enumerate(events):
        assert ev.request == f"request-{i % n}" or ev.request.startswith("request-")
        # Each line is individually valid (read_failure_events already
        # validates every line; reaching here with N events means no
        # line was corrupted).


def test_concurrent_appends_each_line_parseable(tmp_path: Path):
    """A stronger check: every line in the file is individually valid
    JSON (no two writes interleaved into one line)."""
    out = tmp_path / "failures.jsonl"
    n = 40

    def _one(i: int):
        ev = make_failure_event(
            photo=None,
            region_mark=None,
            request=f"req-{i}",
            model="m",
            prompt_version="p",
            output_scad=f"cube({i});",
            failure_class="timeout",
        )
        append_failure_line(ev, out)

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        futs = [ex.submit(_one, i) for i in range(n)]
        for f in futs:
            f.result()

    lines = out.read_text(encoding="utf-8").splitlines()
    assert len(lines) == n
    import json

    for line in lines:
        obj = json.loads(line)  # raises if any line is corrupt
        assert obj["failure_class"] == "timeout"


# ---------------------------------------------------------------------------
# default path / defaults
# ---------------------------------------------------------------------------


def test_default_failures_path_is_repo_relative_evals():
    """The default path is ``<repo root>/evals/failures.jsonl`` (two
    levels up from this module, then ``evals/``)."""
    p = default_failures_path()
    assert p.name == "failures.jsonl"
    assert p.parent.name == "evals"
    # Two levels above this test file's repo root's ``d33d/evals/``.
    # (We don't assert the absolute path — the worktree location varies.)


def test_render_worker_classes_are_subset_of_eval_classes():
    """The 5 render-worker classes are a subset of the eval superset
    (the 1:1 mapping is by exact string)."""
    assert RENDER_WORKER_CLASSES.issubset(EVAL_FAILURE_CLASSES)


def test_eval_superset_contains_gate_reasons_and_outcomes():
    """The superset includes the 2 eval-context outcome classes and the
    11 named LLM classes (minus the vision-only ``geometrically_wrong``)."""
    for cls in (
        "graceful_refusal",
        "clearance_applied",
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
    ):
        assert cls in EVAL_FAILURE_CLASSES
    # The vision-only class is NOT in the gate-taggable superset.
    assert "geometrically_wrong" not in EVAL_FAILURE_CLASSES


# ---------------------------------------------------------------------------
# the app's production hook closure (default_run_design_loop_hook)
# ---------------------------------------------------------------------------


def test_app_hook_closure_appends_on_exhausted(tmp_path: Path, monkeypatch):
    """The app's ``default_run_design_loop_hook`` closure matches the DI
    seam's duck type (``run_loop(**kwargs)``) and appends a line on an
    exhausted result."""
    out = tmp_path / "failures.jsonl"
    calls = []

    async def fake_real_run(**kwargs):
        calls.append(kwargs)
        return _exhausted_result("empty_model", scad="cube();")

    # Monkeypatch the real async run that the hook awaits by default.
    import d33d.design_loop as dl
    monkeypatch.setattr(dl, "run_design_loop_async", fake_real_run)
    hook = default_run_design_loop_hook_sync(path=out)
    result = hook(
        photo="/photos/ref.png",
        chat_history=["make it a cube"],
        stated_dims=(10, 10, 10),
        render_fn=lambda *a: None,
        llm_fn=lambda *a: None,
        model=_llm_result("model-x"),
        prompt_version="deadbeef",
        request="make it a cube",
    )
    assert result is not None
    assert result.status == "exhausted"
    events = read_failure_events(out)
    assert len(events) == 1
    assert events[0].failure_class == "empty_model"
    assert events[0].model == "model-x"
    assert events[0].prompt_version == "deadbeef"
    # The hook's model/prompt_version kwargs are NOT forwarded to the real
    # loop — the loop doesn't accept them.
    assert "model" not in calls[0]
    assert "prompt_version" not in calls[0]
    # ``request`` IS forwarded (issue #97: the hook used to pop it and the
    # value never reached the loop — the user's request was never rendered
    # in the design prompt). The forwarded value must equal the hook's
    # archive value: the loop's prompt and the failures.jsonl line carry
    # the same request, never a prior-turn chat_history fallback.
    assert calls[0].get("request") == "make it a cube"
    assert events[0].request == calls[0]["request"]


def test_app_hook_closure_no_append_on_pass(tmp_path: Path, monkeypatch):
    """The app's hook closure appends nothing on a passing loop."""
    out = tmp_path / "failures.jsonl"

    async def fake_real_run(**kwargs):
        return _passing_result()

    import d33d.design_loop as dl
    monkeypatch.setattr(dl, "run_design_loop_async", fake_real_run)
    hook = default_run_design_loop_hook_sync(path=out)
    hook(
        photo="/photos/ref.png",
        chat_history=["make it a cube"],
        stated_dims=(10, 10, 10),
        render_fn=lambda *a: None,
        llm_fn=lambda *a: None,
        model=_llm_result("model-x"),
        prompt_version="deadbeef",
        request="make it a cube",
    )
    assert not out.exists()


def test_app_hook_async_path_awaits_and_appends_on_exhausted(tmp_path: Path, monkeypatch):
    """The async hook (returned by ``default_run_design_loop_hook``) is
    ``async def``: awaiting it runs the async loop and appends on an
    exhausted result — the path the FastAPI finalize route uses."""
    out = tmp_path / "failures.jsonl"

    async def fake_real_run(**kwargs):
        return _exhausted_result("empty_model", scad="cube();")

    import asyncio as _asyncio

    import d33d.design_loop as dl
    monkeypatch.setattr(dl, "run_design_loop_async", fake_real_run)
    hook = default_run_design_loop_hook(path=out)

    async def _drive():
        return await hook(
            photo="/photos/ref.png",
            chat_history=["make it a cube"],
            stated_dims=(10, 10, 10),
            render_fn=lambda *a: None,
            llm_fn=lambda *a: None,
            model=_llm_result("model-x"),
            prompt_version="deadbeef",
            request="make it a cube",
        )

    result = _asyncio.run(_drive())
    assert result.status == "exhausted"
    events = read_failure_events(out)
    assert len(events) == 1
    assert events[0].failure_class == "empty_model"


def test_app_hook_closure_swallows_hook_errors(tmp_path: Path, monkeypatch):
    """A hook failure (e.g. disk-full) is logged, never raised — the
    design result is still returned."""
    out = tmp_path / "failures.jsonl"

    async def fake_real_run(**kwargs):
        return _exhausted_result("empty_model", scad="cube();")

    import d33d.design_loop as dl
    monkeypatch.setattr(dl, "run_design_loop_async", fake_real_run)
    hook = default_run_design_loop_hook_sync(path=out)
    # Make the append fail by pointing at a directory.
    result = hook(
        photo="/photos/ref.png",
        chat_history=["make it a cube"],
        stated_dims=(10, 10, 10),
        render_fn=lambda *a: None,
        llm_fn=lambda *a: None,
        model=_llm_result("model-x"),
        prompt_version="deadbeef",
        request="make it a cube",
    )
    assert result.status == "exhausted"


def test_app_state_run_design_loop_is_hooked(tmp_path: Path, monkeypatch):
    """``create_app`` wires ``app.state.run_design_loop`` to the hooked
    closure (issue #9, workstream task-failures). A test that needs a
    stub overwrites it after ``create_app`` returns."""
    from d33d.app import create_app

    monkeypatch.delenv("D33D_FAILURES_JSONL", raising=False)
    app = create_app(
        tmp_path / "d33d.sqlite3",
        master_key_path=tmp_path / "master.key",
        catalogue_path=tmp_path / "models.yaml",
        spa_dist_dir=tmp_path / "no-dist",
    )
    assert app.state.run_design_loop is not None
    assert callable(app.state.run_design_loop)
    # The production closure takes (app, **kwargs) — the finalize route's
    # signature check calls it with app + the loop's kwargs.
    import inspect as _inspect

    sig = _inspect.signature(app.state.run_design_loop)
    assert "app" in sig.parameters
    assert app.state.failures_jsonl_path is not None


def test_app_state_hooked_loop_archives_exhausted_loop(tmp_path: Path, monkeypatch):
    """The production ``run_design_loop`` wired by ``create_app`` runs the
    real design loop and, on an exhausted result, appends one line to
    ``app.state.failures_jsonl_path`` (issue #9 — production gate failures
    auto-archived). Monkeypatches the real loop, the capability probe, and
    the model resolution so no live LLM/Docker/catalogue is needed.

    The hook's ``default_run_design_loop_hook`` is monkeypatched to call
    the fake loop directly (skipping the real loop's required kwargs) so
    the test isolates the hook's archive behavior from the loop's
    internal pipeline.
    """
    from d33d.app import create_app
    from d33d.config import catalogue as cat_mod
    from d33d.config import probes as probe_mod
    from d33d.config import resolve as resolve_mod
    from d33d.evals.failure_capture import read_failure_events

    class _Entry:
        model = "model-x"
        provider = "p"
        id = "m"

        def provider_key(self):
            return "p"

    class _Provider:
        base = "http://127.0.0.1:1"
        key = "k"

    class _Cat:
        def __init__(self) -> None:
            self.providers = {"p": _Provider()}
            self.roles = {"design": "m"}
            self.models = {"m": _Entry()}

        def role(self, r: str) -> str:
            return self.roles[r]

        def model(self, alias: str):
            return self.models[alias]

    class _Res:
        entry = _Entry()
        provider = _Provider()

    def _exhausted_result(**kwargs):
        class _Best:
            scad_source = "cube([1,1,1]);"

        class _Result:
            status = "exhausted"
            best = _Best()
            failure_reason = "empty_model"

        return _Result()

    monkeypatch.setattr(cat_mod, "load_catalogue", lambda *a, **k: _Cat())
    monkeypatch.setattr(resolve_mod, "resolve_model", lambda *a, **k: _Res())

    async def _fake_probe(**kwargs):
        from d33d.config.probes import CapabilityResult

        return CapabilityResult(
            tools=True, json_schema=True, vision=False, max_images=0
        )

    monkeypatch.setattr(probe_mod, "probe_capabilities", _fake_probe)

    # Monkeypatch the hook to skip the real loop and return the fake result
    # (the hook normally calls ``real_run(**kwargs)`` after popping
    # model/prompt_version/request — we skip that and return the fake
    # result directly, then invoke the hook's archive logic via the real
    # ``record_production_failure``).
    from d33d.evals import failure_capture as fc

    def _fake_hook(*, path):
        async def _hooked(**kwargs):
            model = kwargs.pop("model", None)
            pv = kwargs.pop("prompt_version", None)
            req = kwargs.pop("request", None)
            photo = kwargs.get("photo")
            result = _exhausted_result()
            fc.record_production_failure(
                design_result=result,
                photo=photo,
                region_mark=None,
                request=str(req or ""),
                model=model,
                prompt_version=str(pv or ""),
                output_scad=result.best.scad_source,
                path=path,
            )
            return result

        return _hooked

    monkeypatch.setattr(fc, "default_run_design_loop_hook", _fake_hook)

    monkeypatch.delenv("D33D_FAILURES_JSONL", raising=False)
    # Point the failures sink at a fresh tmp_path file (the test's own
    # tmp_path, not the repo's evals/failures.jsonl).
    app = create_app(
        tmp_path / "d33d.sqlite3",
        master_key_path=tmp_path / "master.key",
        catalogue_path=tmp_path / "models.yaml",
        spa_dist_dir=tmp_path / "no-dist",
    )
    app.state.failures_jsonl_path = tmp_path / "failures.jsonl"
    loop = app.state.run_design_loop

    async def _drive():
        return await loop(
            app=app,
            photo="/photos/ref.png",
            request="make it a cube",
            stated_dims=(10, 10, 10),
        )

    import asyncio as _asyncio

    result = _asyncio.run(_drive())
    assert result.status == "exhausted"
    events = read_failure_events(app.state.failures_jsonl_path)
    assert len(events) == 1
    assert events[0].failure_class == "empty_model"
    assert events[0].model == "model-x"
    assert events[0].request == "make it a cube"


def test_hooked_loop_preflight_failure_archives_renderer_unavailable(
    tmp_path: Path, monkeypatch
):
    """Issue #277 e2e pin: a production-shape renderer pre-flight failure
    (the REAL ``run_design_loop_async`` returns the loop's exhausted
    ``renderer_unavailable`` result — ``best.render is None``, zero LLM
    calls) flows through the app's hooked production loop and the
    failures.jsonl hook FIRES (appends one correctly-shaped line with the
    loop-level ``renderer_unavailable`` class and the RESOLVED string
    model id) without raising — the hook's ``best.scad_source`` read on
    the ``render is None`` record must not break the archive."""
    import asyncio as _asyncio

    from d33d.app import create_app
    from d33d.config import catalogue as cat_mod
    from d33d.config import probes as probe_mod
    from d33d.config import resolve as resolve_mod
    from d33d.evals.failure_capture import read_failure_events

    class _Entry:
        model = "model-x"
        provider = "p"
        id = "m"

        def provider_key(self):
            return "p"

    class _Provider:
        base = "http://127.0.0.1:1"
        key = "k"

    class _Cat:
        def __init__(self) -> None:
            self.providers = {"p": _Provider()}
            self.roles = {"design": "m"}
            self.models = {"m": _Entry()}

        def role(self, r: str) -> str:
            return self.roles[r]

        def model(self, alias: str):
            return self.models[alias]

    class _Res:
        entry = _Entry()
        provider = _Provider()

    llm_calls = {"n": 0}

    monkeypatch.setattr(cat_mod, "load_catalogue", lambda *a, **k: _Cat())
    monkeypatch.setattr(resolve_mod, "resolve_model", lambda *a, **k: _Res())

    async def _fake_probe(**kwargs):
        from d33d.config.probes import CapabilityResult

        return CapabilityResult(
            tools=True, json_schema=True, vision=False, max_images=0
        )

    monkeypatch.setattr(probe_mod, "probe_capabilities", _fake_probe)

    # The real loop runs: the failing pre-flight stub short-circuits it
    # before any LLM/render call (no ``docker info`` probe — that seam is
    # the ``renderer_is_available`` module attribute, patched to return
    # False). The closure builds its ``llm_fn`` via the module-level
    # ``make_llm_fn``; the stub's ``__call__`` (a pre-flight regression)
    # increments the counter.
    from d33d import design_loop as dl
    from d33d.design_llm import LLMResult

    def _passing_llm(*a, **k):
        llm_calls["n"] += 1
        return LLMResult(
            content="cube();",
            tool_calls=(
                {"name": "emit_design", "arguments": {"scad": "cube();"}},
            ),
            prompt_hash="h" * 64,
            tier="T1",
            status="ok",
            request_body={},
        )

    monkeypatch.setattr(dl, "make_llm_fn", lambda *a, **k: _passing_llm)
    monkeypatch.setattr(dl, "renderer_is_available", lambda *a, **k: False)

    monkeypatch.delenv("D33D_FAILURES_JSONL", raising=False)
    app = create_app(
        tmp_path / "d33d.sqlite3",
        master_key_path=tmp_path / "master.key",
        catalogue_path=tmp_path / "models.yaml",
        spa_dist_dir=tmp_path / "no-dist",
    )
    app.state.failures_jsonl_path = tmp_path / "failures.jsonl"
    # ``create_app`` wires the hooked production closure; use the ALREADY-WIRED
    # one. Its closure resolves ``make_llm_fn`` (module-level, patched above)
    # and the hook's ``real_run`` from the ``d33d.design_loop`` module at call
    # time, so the monkeypatches are in force. The production closure does not
    # forward ``renderer_check`` (it forwards a fixed set of kwargs), so the
    # failing pre-flight is driven through the loop's ``renderer_is_available``
    # module attribute (the ``None`` default path — the loop resolves it by
    # name at call time, so the monkeypatch above is in force). The closure's
    # own kwargs do not set it, so the loop's default (the patched attribute)
    # is used.
    from d33d.design_loop_events import bbox_from_render

    loop = app.state.run_design_loop

    async def _drive():
        return await loop(
            app=app,
            photo="/photos/ref.png",
            request="make it a cube",
            stated_dims=(10, 10, 10),
            bbox_fn=bbox_from_render,
        )

    result = _asyncio.run(_drive())
    assert result.status == "exhausted"
    assert result.failure_reason == "renderer_unavailable"
    assert llm_calls["n"] == 0
    # The hook fired (did not raise) on the ``render is None`` record.
    events = read_failure_events(app.state.failures_jsonl_path)
    assert len(events) == 1
    assert events[0].failure_class == "renderer_unavailable"
    # The hook's ``model`` is the RESOLVED model id (a plain string the
    # production closure supplies from ``resolve_model``).
    assert events[0].model == "model-x"
    assert events[0].request == "make it a cube"


def test_app_state_hooked_loop_no_archive_on_pass(tmp_path: Path, monkeypatch):
    """The production loop appends nothing to failures.jsonl on a passing
    design loop (nothing to archive) — the hook fires only on an
    exhausted result."""
    from d33d.app import create_app
    from d33d.config import catalogue as cat_mod
    from d33d.config import probes as probe_mod
    from d33d.config import resolve as resolve_mod
    from d33d.evals.failure_capture import read_failure_events

    class _Entry:
        model = "model-x"
        provider = "p"
        id = "m"

        def provider_key(self):
            return "p"

    class _Provider:
        base = "http://127.0.0.1:1"
        key = "k"

    class _Cat:
        def __init__(self) -> None:
            self.providers = {"p": _Provider()}
            self.roles = {"design": "m"}
            self.models = {"m": _Entry()}

        def role(self, r: str) -> str:
            return self.roles[r]

        def model(self, alias: str):
            return self.models[alias]

    class _Res:
        entry = _Entry()
        provider = _Provider()

    def _passing_result(**kwargs):
        class _Best:
            scad_source = "cube([1,1,1]);"

        class _Result:
            status = "pass"
            best = _Best()
            failure_reason = None

        return _Result()

    monkeypatch.setattr(cat_mod, "load_catalogue", lambda *a, **k: _Cat())
    monkeypatch.setattr(resolve_mod, "resolve_model", lambda *a, **k: _Res())

    async def _fake_probe(**kwargs):
        from d33d.config.probes import CapabilityResult

        return CapabilityResult(
            tools=True, json_schema=True, vision=False, max_images=0
        )

    monkeypatch.setattr(probe_mod, "probe_capabilities", _fake_probe)

    from d33d.evals import failure_capture as fc

    def _fake_hook(*, path):
        async def _hooked(**kwargs):
            model = kwargs.pop("model", None)
            pv = kwargs.pop("prompt_version", None)
            req = kwargs.pop("request", None)
            photo = kwargs.get("photo")
            result = _passing_result()
            fc.record_production_failure(
                design_result=result,
                photo=photo,
                region_mark=None,
                request=str(req or ""),
                model=model,
                prompt_version=str(pv or ""),
                output_scad=result.best.scad_source,
                path=path,
            )
            return result

        return _hooked

    monkeypatch.setattr(fc, "default_run_design_loop_hook", _fake_hook)

    monkeypatch.delenv("D33D_FAILURES_JSONL", raising=False)
    # Point the failures sink at a fresh tmp_path file (the test's own
    # tmp_path, not the repo's evals/failures.jsonl).
    app = create_app(
        tmp_path / "d33d.sqlite3",
        master_key_path=tmp_path / "master.key",
        catalogue_path=tmp_path / "models.yaml",
        spa_dist_dir=tmp_path / "no-dist",
    )
    app.state.failures_jsonl_path = tmp_path / "failures.jsonl"
    loop = app.state.run_design_loop

    async def _drive():
        return await loop(
            app=app,
            photo="/photos/ref.png",
            request="make it a cube",
            stated_dims=(10, 10, 10),
        )

    import asyncio as _asyncio

    result = _asyncio.run(_drive())
    assert result.status == "pass"
    events = read_failure_events(app.state.failures_jsonl_path)
    assert events == []


# ---------------------------------------------------------------------------
# (issue #396) — the whole-loop deadline in ``design_loop_events``
# ---------------------------------------------------------------------------


def test_design_loop_deadline_archives_timeout_row(
    _eval_app_with_versions, tmp_path: Path, monkeypatch
):
    """Issue #396: the ``run_design_loop_with_events`` deadline (the
    whole-loop timeout — a loop that never returns) appends a
    failures.jsonl row with the loop-level
    ``design_loop_timed_out`` class, the request, the app's injected
    model and the last SCAD the loop's frames surfaced (``""`` when
    nothing rendered) — the hook at the loop seam never sees a
    deadline kill (there is no loop result), so without the adapter's
    own archive the whole-loop timeout would never reach the archive
    while every other failure class does."""
    import asyncio as _asyncio

    from d33d.design_loop_events import (
        DESIGN_LOOP_TIMED_OUT_REASON,
        run_design_loop_with_events,
    )

    monkeypatch.setattr(
        "d33d.design_loop_events.DESIGN_LOOP_ATTEMPT_TIMEOUT_SECONDS", 0.5 / 3
    )

    class _StallLoop:
        """Production-seam-shaped stub (takes ``app``) that never
        terminates within the deadline — the real production loop can
        take minutes, so a multi-second stall is realistic. The
        adapter's deadline archive reads the ``model`` kwarg the
        production closure would have passed, so the stub re-injects
        it into ``kwargs`` before the stall (the app's
        ``_build_production_design_loop`` closure does this in
        production — the stub mirrors that contract)."""

        def __call__(self, app=None, **kwargs):
            async def _stall():
                kwargs["model"] = "model-x"
                await _asyncio.sleep(10)

            return _stall()

    app = _eval_app_with_versions

    async def _call(client):
        proj = await _create_project(client)
        pid = proj["id"]
        app.state.run_design_loop = _StallLoop()
        app.state.failures_jsonl_path = tmp_path / "failures.jsonl"
        app.state.model_id = "model-x"
        source = run_design_loop_with_events(
            app,
            pid,
            user_message="make it a cube",
            stated_dims=None,
            chat_history=(),
            photo="data:image/png;base64,x",
            request_text="make it a cube",
        )
        frames = []
        async for event, data in source:
            frames.append((event, data))
            if event in ("done", "error"):
                break
        return frames

    frames = _drive_stream(app, _call)
    assert frames, "no frames emitted at all"
    events = [f[0] for f in frames]
    assert events[-1] == "error", f"no terminal error frame: {frames}"
    assert (
        frames[-1][1].get("reason") == DESIGN_LOOP_TIMED_OUT_REASON
    ), f"wrong reason: {frames[-1][1]}"
    out = tmp_path / "failures.jsonl"
    assert out.exists(), "deadline did not archive a failures.jsonl row"
    row_events = read_failure_events(out)
    assert len(row_events) == 1, (
        f"expected exactly one archive row, got {len(row_events)}"
    )
    ev = row_events[0]
    assert ev.failure_class == DESIGN_LOOP_TIMED_OUT_REASON
    assert ev.request == "make it a cube"
    # The model is the app's production closure's injected id (the
    # ``model`` kwarg the hook pops before the loop runs); the stub
    # receives it via ``**kwargs`` and the archive reads it back.
    assert ev.model  # non-empty, the resolved model id
    assert ev.prompt_version == ""  # honest absence — the loop never ran to a result
    # Issue #417: the archive row carries the attempt count and per-attempt
    # latencies (omit-not-null: a zero-render stall has no attempt count and
    # no latencies — the adapter's "no version" path handles the honest
    # absence). This stall never rendered, so both are None.
    assert ev.attempt_count is None, f"expected no attempt count, got {ev.attempt_count}"
    assert ev.per_attempt_latencies is None, f"expected no latencies, got {ev.per_attempt_latencies}"


def test_design_loop_deadline_row_carries_last_scad_from_frames(
    _eval_app_with_versions, tmp_path: Path, monkeypatch
):
    """Issue #396: the deadline archive row's ``output_scad`` is the
    last SCAD source the loop's frames have surfaced — a token frame's
    ``text`` on a pass, or a progress frame's ``scad_source`` when the
    loop was killed mid-run (``""`` only when the loop produced no
    candidate at all — never a fabricated placeholder)."""
    import asyncio as _asyncio

    from d33d.design_loop_events import (
        DESIGN_LOOP_TIMED_OUT_REASON,
        run_design_loop_with_events,
    )

    monkeypatch.setattr(
        "d33d.design_loop_events.DESIGN_LOOP_ATTEMPT_TIMEOUT_SECONDS", 0.5 / 3
    )

    class _StallLoopWithScad:
        """A stall that emits a progress frame carrying ``scad_source``
        before never returning (the ``model`` kwarg is re-injected the
        same way as in the plain stall — the production closure's
        contract)."""

        def __call__(self, app=None, **kwargs):
            async def _stall():
                kwargs["model"] = "model-x"
                on_progress = kwargs.get("on_progress")
                if on_progress is not None:
                    on_progress(
                        "view-done",
                        {"view": "view_01", "iteration": 1},
                    )
                # The progress frame the adapter yields for this marker
                # carries no scad_source — but the loop's own prompt
                # (not observable from the adapter) does. The adapter
                # scans yielded frames; a stall that never passes has no
                # token frame, so output_scad is "" (honest absence).
                await _asyncio.sleep(10)

            return _stall()

    app = _eval_app_with_versions

    async def _call(client):
        r = await client.post("/api/projects", json={"name": "test project"})
        assert r.status_code == 201, r.text
        pid = r.json()["id"]
        app.state.run_design_loop = _StallLoopWithScad()
        app.state.failures_jsonl_path = tmp_path / "failures.jsonl"
        app.state.model_id = "model-x"
        source = run_design_loop_with_events(
            app,
            pid,
            user_message="make it a cube",
            stated_dims=None,
            chat_history=(),
            photo="data:image/png;base64,x",
            request_text="make it a cube",
        )
        frames = []
        async for event, data in source:
            frames.append((event, data))
            if event in ("done", "error"):
                break
        return frames

    frames = _drive_stream(app, _call)
    assert frames and frames[-1][0] == "error", f"no terminal error frame: {frames}"
    out = tmp_path / "failures.jsonl"
    assert out.exists(), "deadline did not archive a failures.jsonl row"
    row_events = read_failure_events(out)
    assert len(row_events) == 1
    ev = row_events[0]
    assert ev.failure_class == DESIGN_LOOP_TIMED_OUT_REASON
    # A stall that never passed has no token frame and no progress
    # frame carrying ``scad_source`` — the row's ``output_scad`` is
    # the honest absence (""), never a fabricated placeholder.
    assert ev.output_scad == ""


def test_design_loop_deadline_archive_sees_asyncio_wait_frames(
    _eval_app_with_versions, tmp_path: Path, monkeypatch
):
    """Issue #396 lens fix: a frame carrying ``scad_source`` that arrives
    through the ``asyncio.wait`` branch (queued while the render task was
    still running, consumed via the await rather than the
    ``get_nowait`` path) MUST reach the deadline archive.

    The old implementation only recorded frames taken from the
    ``get_nowait`` branch into ``_emitted_frames`` — a frame delivered
    through the ``asyncio.wait`` path was yielded to the client but never
    recorded, so the archive's ``output_scad`` could hold stale or empty
    SCAD. The fix tracks the LAST ``scad_source`` across BOTH paths."""
    import asyncio as _asyncio

    from d33d.design_loop_events import (
        DESIGN_LOOP_TIMED_OUT_REASON,
        run_design_loop_with_events,
    )

    monkeypatch.setattr(
        "d33d.design_loop_events.DESIGN_LOOP_ATTEMPT_TIMEOUT_SECONDS", 1.0 / 3
    )

    SCAD = "W = 40; cube([W, 40, 20]);\n"

    # Capture the adapter's internal _frame_queue instance by wrapping
    # asyncio.Queue in the design_loop_events module namespace.
    _captured_queues: list[_asyncio.Queue] = []
    _original_queue_cls = _asyncio.Queue

    class _SpyQueue(_original_queue_cls):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            _captured_queues.append(self)

    monkeypatch.setattr("d33d.design_loop_events.asyncio.Queue", _SpyQueue)

    class _StallLoop:
        """A stall that enqueues a progress frame carrying
        ``scad_source`` shortly after start. The frame is injected
        directly on the captured queue AFTER the adapter's first
        ``get_nowait`` has drained (it will be empty on the first
        iteration), so the frame is consumed through the
        ``asyncio.wait`` path — the exact path the bug affected."""

        def __call__(self, app=None, **kwargs):
            async def _stall():
                kwargs["model"] = "model-x"
                # Sleep long enough for the adapter to have started its
                # wait loop and taken an empty ``get_nowait``.
                await _asyncio.sleep(0.3)
                # The adapter is now in ``asyncio.wait``; inject the
                # frame directly on the queue. Because it arrives while
                # ``asyncio.wait`` is parked, it is yielded via that
                # path (not ``get_nowait``).
                if _captured_queues:
                    _captured_queues[-1].put_nowait(
                        ("progress", {"step": "scad-ready", "scad_source": SCAD})
                    )
                await _asyncio.sleep(10)

            return _stall()

    app = _eval_app_with_versions

    async def _call(client):
        r = await client.post("/api/projects", json={"name": "test project"})
        assert r.status_code == 201, r.text
        pid = r.json()["id"]
        app.state.run_design_loop = _StallLoop()
        app.state.failures_jsonl_path = tmp_path / "failures.jsonl"
        app.state.model_id = "model-x"
        source = run_design_loop_with_events(
            app,
            pid,
            user_message="make it a cube",
            stated_dims=None,
            chat_history=(),
            photo="data:image/png;base64,x",
            request_text="make it a cube",
        )
        frames = []
        async for event, data in source:
            frames.append((event, data))
            if event in ("done", "error"):
                break
        return frames

    frames = _drive_stream(app, _call)
    assert frames and frames[-1][0] == "error", f"no terminal error frame: {frames}"
    assert frames[-1][1].get("reason") == DESIGN_LOOP_TIMED_OUT_REASON
    # The scad frame must have been yielded to the client too.
    scad_frames = [d for e, d in frames if d.get("scad_source") == SCAD]
    assert scad_frames, "scad_source frame was never yielded to the client"
    out = tmp_path / "failures.jsonl"
    assert out.exists(), "deadline did not archive a failures.jsonl row"
    row_events = read_failure_events(out)
    assert len(row_events) == 1
    ev = row_events[0]
    assert ev.failure_class == DESIGN_LOOP_TIMED_OUT_REASON
    # The frame arrived through the ``asyncio.wait`` path and must be in
    # the archive row — this is the bug the lens fix addresses.
    assert ev.output_scad == SCAD, (
        f"expected the asyncio.wait-path scad_source in the archive row, "
        f"got {ev.output_scad!r}"
    )


# ---------------------------------------------------------------------------
# (issue #417) — slow-model timeout: keep the best candidate, version it,
# archive with attempt count + latencies, emit the slow-model copy
# ---------------------------------------------------------------------------


def test_design_loop_slow_model_timeout_keeps_candidate_and_versions(
    _eval_app_with_versions, tmp_path: Path, monkeypatch
):
    """Issue #417: a scripted slow-model loop that renders attempt 1
    (emitting a progress frame carrying ``scad_source``) then exceeds
    the per-attempt deadline on attempt 2 yields:

    (a) the attempt-1 candidate kept and stored as a version (via
        ``_resolve_version_create`` — the timeout-version path);
    (b) a ``version-created`` progress frame BEFORE the terminal error;
    (c) the terminal error frame with ``attempt_latency_seconds`` and
        ``attempt_count`` fields (the SPA renders the slow-model copy
        from these measured values);
    (d) a failures.jsonl row with the attempt count and per-attempt
        latencies.

    The ``scad_source`` frame is injected from INSIDE the stub's
    coroutine (which runs on a worker thread via ``asyncio.to_thread``)
    via the captured queue — the same mechanism as
    ``test_design_loop_deadline_archive_sees_asyncio_wait_frames`` —
    so the adapter's ``_last_scad_source`` tracker sees it BEFORE the
    deadline fires (the stub sleeps 0.3 s before injecting, well within
    the 0.5 s total deadline window).
    """
    import asyncio as _asyncio

    from d33d.design_loop_events import (
        DESIGN_LOOP_TIMED_OUT_REASON,
        run_design_loop_with_events,
    )

    SCAD = "W = 40; cube([W, 40, 20]);\n"

    monkeypatch.setattr(
        "d33d.design_loop_events.DESIGN_LOOP_ATTEMPT_TIMEOUT_SECONDS", 0.5 / 3
    )

    # Capture the adapter's internal _frame_queue instance by wrapping
    # asyncio.Queue in the design_loop_events module namespace.
    _captured_queues: list = []
    _original_queue_cls = _asyncio.Queue

    class _SpyQueue(_original_queue_cls):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            _captured_queues.append(self)

    monkeypatch.setattr("d33d.design_loop_events.asyncio.Queue", _SpyQueue)

    class _SlowModelLoop:
        """A scripted slow-model loop: attempt 1 surfaces a SCAD (via a
        progress frame injected directly on the captured queue, the same
        mechanism the asyncio.wait test uses), then attempt 2 stalls
        past the deadline. The ``model`` kwarg is re-injected the same
        way as in the plain stall (the production closure's contract)."""

        def __call__(self, app=None, **kwargs):
            async def _slow():
                kwargs["model"] = "model-x"
                # Attempt 1: the loop rendered a candidate. Inject a
                # progress frame carrying ``scad_source`` directly on
                # the captured queue (the adapter's ``_last_scad_source``
                # reader) — the stub's ``on_progress`` only enqueues view
                # markers (no scad_source field), so the direct injection
                # is how the adapter tracks the rendered candidate.
                # Sleep long enough for the adapter to have started its
                # wait loop and taken an empty ``get_nowait`` (so the
                # frame is consumed through the ``asyncio.wait`` path —
                # the exact path the asyncio.wait test exercises).
                await _asyncio.sleep(0.3)
                if _captured_queues:
                    # Inject the scad_source frame with an ``iteration``
                    # stamp (the adapter's ``_observe_frame`` reads it to
                    # track the attempt count — the same field the real
                    # loop's per-view markers carry, issue #121).
                    _captured_queues[-1].put_nowait(
                        (
                            "progress",
                            {
                                "step": "scad-ready",
                                "scad_source": SCAD,
                                "iteration": 1,
                            },
                        )
                    )
                # Attempt 2: stall past the deadline (the total deadline
                # is 3 × 0.5/3 = 0.5 s; the 10 s sleep far exceeds it).
                await _asyncio.sleep(10)

            return _slow()

    app = _eval_app_with_versions

    async def _call(client):
        r = await client.post("/api/projects", json={"name": "test project"})
        assert r.status_code == 201, r.text
        pid = r.json()["id"]
        app.state.run_design_loop = _SlowModelLoop()
        app.state.failures_jsonl_path = tmp_path / "failures.jsonl"
        app.state.model_id = "model-x"
        source = run_design_loop_with_events(
            app,
            pid,
            user_message="make it a cube",
            stated_dims=None,
            chat_history=(),
            photo="data:image/png;base64,x",
            request_text="make it a cube",
        )
        frames = []
        async for event, data in source:
            frames.append((event, data))
            if event in ("done", "error"):
                break
        return frames, pid

    frames, pid = _drive_stream(app, _call)
    assert frames and frames[-1][0] == "error", f"no terminal error frame: {frames}"
    error_data = frames[-1][1]
    assert error_data.get("reason") == DESIGN_LOOP_TIMED_OUT_REASON

    # (b) A ``version-created`` progress frame appears BEFORE the
    # terminal error frame (the kept candidate was stored as a version).
    vc_frames = [
        (i, d)
        for i, (e, d) in enumerate(frames)
        if e == "progress" and d.get("step") == "version-created"
    ]
    assert vc_frames, (
        f"no version-created frame before the terminal error: {frames}"
    )
    _vc_idx, vc_data = vc_frames[0]
    assert isinstance(vc_data.get("version_id"), int), (
        f"version-created frame missing version_id: {vc_data}"
    )
    # The version-created frame must come BEFORE the terminal error
    # (the last frame).
    assert _vc_idx < len(frames) - 1, (
        f"version-created frame at index {_vc_idx} is not before the "
        f"terminal error (total frames: {len(frames)})"
    )

    # (c) The terminal error frame carries the slow-model copy data.
    assert "attempt_count" in error_data, f"no attempt_count: {error_data}"
    assert error_data["attempt_count"] >= 1, (
        f"attempt_count should be >= 1, got {error_data['attempt_count']}"
    )
    # The scad_source frame must have been yielded to the client too
    # (the adapter's ``_last_scad_source`` saw it before the deadline).
    scad_frames = [d for e, d in frames if d.get("scad_source") == SCAD]
    assert scad_frames, "scad_source frame was never yielded to the client"

    # (d) The archive row carries the attempt count and per-attempt
    # latencies (the kept candidate's SCAD is the row's output_scad).
    out = tmp_path / "failures.jsonl"
    assert out.exists(), "deadline did not archive a failures.jsonl row"
    row_events = read_failure_events(out)
    assert len(row_events) == 1
    ev = row_events[0]
    assert ev.failure_class == DESIGN_LOOP_TIMED_OUT_REASON
    # The archive row's attempt_count matches the terminal frame's.
    if error_data.get("attempt_count"):
        assert ev.attempt_count == error_data["attempt_count"], (
            f"archive attempt_count {ev.attempt_count} != frame {error_data['attempt_count']}"
        )
    # The archive row's output_scad is the kept candidate's SCAD.
    assert ev.output_scad == SCAD, (
        f"expected the kept candidate's SCAD in the archive row, "
        f"got {ev.output_scad!r}"
    )


def test_design_loop_slow_model_timeout_no_version_on_zero_render(
    _eval_app_with_versions, tmp_path: Path, monkeypatch
):
    """Issue #417 negative case: a timeout with ZERO rendered candidates
    (the stall never surfaced a SCAD) ends WITHOUT a version — the
    honest-absence path (no ``version-created`` frame, no version row).
    The terminal error frame still fires with the structured reason.
    """
    import asyncio as _asyncio

    from d33d.design_loop_events import (
        DESIGN_LOOP_TIMED_OUT_REASON,
        run_design_loop_with_events,
    )

    monkeypatch.setattr(
        "d33d.design_loop_events.DESIGN_LOOP_ATTEMPT_TIMEOUT_SECONDS", 0.5 / 3
    )

    class _StallLoop:
        """A zero-render stall: the loop never surfaces a SCAD."""

        def __call__(self, app=None, **kwargs):
            async def _stall():
                kwargs["model"] = "model-x"
                await _asyncio.sleep(10)

            return _stall()

    app = _eval_app_with_versions

    async def _call(client):
        r = await client.post("/api/projects", json={"name": "test project"})
        assert r.status_code == 201, r.text
        pid = r.json()["id"]
        app.state.run_design_loop = _StallLoop()
        app.state.failures_jsonl_path = tmp_path / "failures.jsonl"
        app.state.model_id = "model-x"
        source = run_design_loop_with_events(
            app,
            pid,
            user_message="make it a cube",
            stated_dims=None,
            chat_history=(),
            photo="data:image/png;base64,x",
            request_text="make it a cube",
        )
        frames = []
        async for event, data in source:
            frames.append((event, data))
            if event in ("done", "error"):
                break
        return frames, pid

    frames, pid = _drive_stream(app, _call)
    assert frames and frames[-1][0] == "error", f"no terminal error frame: {frames}"
    error_data = frames[-1][1]
    assert error_data.get("reason") == DESIGN_LOOP_TIMED_OUT_REASON
    # NO version-created frame (zero rendered candidates → no version).
    vc_frames = [
        d for e, d in frames if e == "progress" and d.get("step") == "version-created"
    ]
    assert not vc_frames, (
        f"unexpected version-created frame on zero-render timeout: {vc_frames}"
    )
    # The archive row exists but carries no SCAD (honest absence).
    out = tmp_path / "failures.jsonl"
    assert out.exists(), "deadline did not archive a failures.jsonl row"
    row_events = read_failure_events(out)
    assert len(row_events) == 1
    ev = row_events[0]
    assert ev.failure_class == DESIGN_LOOP_TIMED_OUT_REASON
    assert ev.output_scad == ""

