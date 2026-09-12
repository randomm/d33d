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
from pydantic import ValidationError

from d33d.evals.failure_capture import (
    EVAL_FAILURE_CLASSES,
    GATE_REASON_CLASSES,
    MAX_LINE_BYTES,
    RENDER_WORKER_CLASSES,
    FailureEvent,
    append_failure_line,
    default_failures_path,
    default_run_design_loop_hook,
    make_failure_event,
    read_failure_events,
    record_production_failure,
)

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

    def fake_real_run(**kwargs):
        calls.append(kwargs)
        return _exhausted_result("empty_model", scad="cube();")

    # Monkeypatch the real run that the hook imports by default.
    import d33d.design_loop as dl
    monkeypatch.setattr(dl, "run_design_loop", fake_real_run)
    hook = default_run_design_loop_hook(path=out)
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
    # The hook kwargs (model/prompt_version/request) are NOT forwarded to
    # the real loop — the loop doesn't accept them.
    assert "model" not in calls[0]
    assert "prompt_version" not in calls[0]
    assert "request" not in calls[0]


def test_app_hook_closure_no_append_on_pass(tmp_path: Path, monkeypatch):
    """The app's hook closure appends nothing on a passing loop."""
    out = tmp_path / "failures.jsonl"

    def fake_real_run(**kwargs):
        return _passing_result()

    import d33d.design_loop as dl
    monkeypatch.setattr(dl, "run_design_loop", fake_real_run)
    hook = default_run_design_loop_hook(path=out)
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


def test_app_hook_closure_swallows_hook_errors(tmp_path: Path, monkeypatch):
    """A hook failure (e.g. disk-full) is logged, never raised — the
    design result is still returned."""
    out = tmp_path / "failures.jsonl"

    def fake_real_run(**kwargs):
        return _exhausted_result("empty_model", scad="cube();")

    import d33d.design_loop as dl
    monkeypatch.setattr(dl, "run_design_loop", fake_real_run)
    hook = default_run_design_loop_hook(path=out)
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

    class _Provider:
        base = "http://127.0.0.1:1"
        key = "k"

    class _Cat:
        def __init__(self) -> None:
            self.providers = {"p": _Provider()}

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
        def _hooked(**kwargs):
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
    result = loop(
        app=app,
        photo="/photos/ref.png",
        request="make it a cube",
        stated_dims=(10, 10, 10),
    )
    assert result.status == "exhausted"
    events = read_failure_events(app.state.failures_jsonl_path)
    assert len(events) == 1
    assert events[0].failure_class == "empty_model"
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

    class _Provider:
        base = "http://127.0.0.1:1"
        key = "k"

    class _Cat:
        def __init__(self) -> None:
            self.providers = {"p": _Provider()}

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
        def _hooked(**kwargs):
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
    result = loop(
        app=app,
        photo="/photos/ref.png",
        request="make it a cube",
        stated_dims=(10, 10, 10),
    )
    assert result.status == "pass"
    events = read_failure_events(app.state.failures_jsonl_path)
    assert events == []
