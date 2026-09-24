"""The promptfoo config is valid YAML and references the 20 golden-set
cases + the 5 hash-pinned prompt files (issue #9, workstream
task-harness).

Covers:
- the config loads via ``load_promptfoo_config`` (valid YAML,
  OpenAI-compatible provider, cases_dir with >= 20 cases, prompts_dir
  with >= 1 prompt, python custom asserts through evals/run.py with the
  deterministic-gates assert present — the 7-gates-before-judge
  ordering the spec pins)
- the 20 case files and 5 prompt files the config references are all on
  disk, each case pinned to a prompt by content hash
"""

from __future__ import annotations

from pathlib import Path

import pytest

from d33d.evals.case_schema import load_golden_set
from d33d.evals.harness import load_promptfoo_config

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = REPO_ROOT / "evals" / "promptfoo.config.yaml"


def _load_config() -> dict:
    return load_promptfoo_config(CONFIG_PATH, REPO_ROOT)


def test_config_is_valid_yaml() -> None:
    """``load_promptfoo_config`` returns a dict (the config parses)."""
    doc = _load_config()
    assert isinstance(doc, dict)


def test_config_names_an_openai_compatible_provider() -> None:
    doc = _load_config()
    providers = doc["providers"]
    assert len(providers) >= 1
    for provider in providers:
        provider_type = provider.get("provider") or provider.get("type")
        assert provider_type.startswith("openai")
        assert provider.get("id")


def test_config_references_20_cases() -> None:
    """The config's ``cases_dir`` holds at least 20 case files (the
    golden-set floor) and ``load_promptfoo_config`` enforces it."""
    doc = _load_config()
    cases_dir = REPO_ROOT / doc["cases_dir"]
    case_files = sorted(cases_dir.glob("*.json"))
    assert len(case_files) >= 20


def test_config_references_the_hash_pinned_prompts() -> None:
    """The config's ``prompts_dir`` holds the 5 hash-pinned prompt
    files; every case pins one of them by content hash."""
    doc = _load_config()
    prompts_dir = REPO_ROOT / doc["prompts_dir"]
    prompt_files = sorted(prompts_dir.glob("*.md"))
    # 5 design-loop golden-set prompts + the question-answer stage-2
    # prompt (issue #249) — every golden case pins one of the first five;
    # the question-answer prompt is pinned by its own fixture (see
    # tests/evals/test_question_answer_fixture.py).
    assert len(prompt_files) >= 5, f"expected >= 5 prompt files, got {len(prompt_files)}"

    # Every case's pin path points into the config's prompts_dir.
    cases = load_golden_set(REPO_ROOT / doc["cases_dir"], REPO_ROOT)
    for case in cases.values():
        pin_path = REPO_ROOT / case.prompt.path
        assert pin_path.is_file()
        assert prompts_dir in pin_path.parents or pin_path.parent == prompts_dir


def test_config_python_asserts_point_at_run_py_with_gates_first() -> None:
    """The config's custom asserts are python asserts through
    ``evals/run.py`` with the deterministic-gates assert present.

    The assert name ``deterministic_gates_pass`` is unchanged, but its
    MEANING changed with issue #108's re-ordering: it now means "the 7
    gates ran on the MODEL'S OUTPUT, rendered through the real render
    worker, AFTER the design call" — previously it meant "the gates
    (run against a null render + a locally-openSCAD'd copy of the
    case's OWN reference .scad) passed", i.e. the golden set measured
    that the reference fixtures were valid, which is a vacuous
    measurement of the model. The config only pins the assert's
    presence, not the ordering — the ordering now lives in
    ``d33d.evals.harness.run_case`` (design call -> render_fn(scad) ->
    gates -> judge) and is pinned structurally (the harness has no
    render_result/mesh/stl_path parameters any more) plus in
    ``tests/evals/test_adversarial_cases.py`` (``render.handed``
    carries the design call's output).
    """
    doc = _load_config()
    asserts = doc["defaults"]["assert"]
    assert asserts, "no asserts"
    for item in asserts:
        assert item["type"] == "python"
        assert item["path"] == "evals/run.py"
    values = [item["value"] for item in asserts]
    assert "deterministic_gates_pass" in values


def test_config_missing_is_file_not_found() -> None:
    with pytest.raises(FileNotFoundError):
        load_promptfoo_config(REPO_ROOT / "nope.yaml", REPO_ROOT)


def test_config_rejects_non_openai_provider(tmp_path: Path) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text(
        "providers:\n  - id: some-model\n    provider: ollama\ncases_dir: c\nprompts_dir: p\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="OpenAI-compatible"):
        load_promptfoo_config(bad, tmp_path)


def test_config_rejects_too_few_cases(tmp_path: Path) -> None:
    cases = tmp_path / "cases"
    cases.mkdir()
    (cases / "a.json").write_text("{}", encoding="utf-8")
    prompts = tmp_path / "prompts"
    prompts.mkdir()
    (prompts / "p.md").write_text("# prompt", encoding="utf-8")
    cfg = tmp_path / "cfg.yaml"
    cfg.write_text(
        "providers:\n  - id: m\n    provider: openai:completions\n"
        f"cases_dir: {cases}\nprompts_dir: {prompts}\n"
        "defaults:\n  assert:\n    - type: python\n      path: evals/run.py\n"
        "      value: deterministic_gates_pass\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="floor is 20"):
        load_promptfoo_config(cfg, tmp_path)


def test_config_rejects_missing_gates_assert(tmp_path: Path) -> None:
    cases = tmp_path / "cases"
    cases.mkdir()
    for i in range(20):
        (cases / f"c{i}.json").write_text("{}", encoding="utf-8")
    prompts = tmp_path / "prompts"
    prompts.mkdir()
    (prompts / "p.md").write_text("# prompt", encoding="utf-8")
    cfg = tmp_path / "cfg.yaml"
    cfg.write_text(
        "providers:\n  - id: m\n    provider: openai:completions\n"
        f"cases_dir: {cases}\nprompts_dir: {prompts}\n"
        "defaults:\n  assert:\n    - type: python\n      path: evals/run.py\n"
        "      value: vision_judge_pass\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="deterministic_gates_pass"):
        load_promptfoo_config(cfg, tmp_path)


# ---------------------------------------------------------------------------
# The two promptfoo custom asserts (the CLI calls these via evals/run.py)
# ---------------------------------------------------------------------------


_RUN_PY = REPO_ROOT / "evals" / "run.py"


def _load_run_module():
    """Import ``evals/run.py`` as a module (the tests run from the repo
    root, but the module lives at ``evals/run.py`` — not a package).
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location("evals_run", _RUN_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_vision_judge_pass_passes_when_judge_none() -> None:
    """A gate-passing candidate with no judge verdict passes (not a
    false-negative): the judge only runs for candidates whose declared
    gates let them through (``CaseOutcome.ok`` is the source of truth)."""
    run = _load_run_module()
    row = {"gates": {"compile": {"status": "pass"}}, "judge": None}
    assert run.vision_judge_pass(row, {}, {}) is True


def test_vision_judge_pass_passes_when_judge_passed() -> None:
    run = _load_run_module()
    row = {"gates": {"compile": {"status": "pass"}}, "judge": {"passed": True}}
    assert run.vision_judge_pass(row, {}, {}) is True


def test_vision_judge_pass_fails_when_judge_failed() -> None:
    run = _load_run_module()
    row = {"gates": {"compile": {"status": "pass"}}, "judge": {"passed": False}}
    assert run.vision_judge_pass(row, {}, {}) is False


def test_deterministic_gates_pass_passes_when_all_gates_pass() -> None:
    run = _load_run_module()
    row = {"gates": {"compile": {"status": "pass"}, "stl_export": {"status": "pass"}}}
    assert run.deterministic_gates_pass(row, {}, {}) is True


def test_deterministic_gates_pass_fails_when_any_gate_fails() -> None:
    run = _load_run_module()
    row = {"gates": {"compile": {"status": "fail"}, "stl_export": {"status": "pass"}}}
    assert run.deterministic_gates_pass(row, {}, {}) is False


def test_deterministic_gates_pass_passes_when_no_gates() -> None:
    run = _load_run_module()
    row = {"gates": {}}
    assert run.deterministic_gates_pass(row, {}, {}) is True


# ---------------------------------------------------------------------------
# Local-run fixture bound tests (evals/run.py --run path) — REMOVED for
# issue #108. The reference-fixture render path (``_local_render_inputs``
# / ``_null_render`` / ``_scad_to_mesh`` / ``MAX_FIXTURE_IMAGE_BYTES``)
# is gone from evals/run.py: the harness's gates now run on the MODEL'S
# OUTPUT rendered through the injected render_fn, never on the case's
# own reference fixture. The three tests below used to pin that path's
# path-escape / over-size bounds; they are deleted with the path they
# guarded (the image-size bound is a fixture-sent-to-the-LLM concern;
# the harness's own message assembly still inlines image parts, and the
# live-suite equivalents live in tests/live_e2e/).
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# N/A marker alignment (GATE_NA_MARKERS vs the gate modules' NA_REASON)
# ---------------------------------------------------------------------------


def test_gate_na_markers_match_gate_modules() -> None:
    """``GATE_NA_MARKERS`` pins the exact N/A string each gate reports —
    a mismatch would make a test asserting the pin against the gate's
    output fail (the pin and the gate's ``NA_REASON`` must agree)."""
    from d33d.evals.case_schema import GATE_NA_MARKERS
    from d33d.evals.region_gate import NA_REASON as REGION_NA
    from d33d.evals.slice_gate import NA_REASON as SLICE_NA

    assert GATE_NA_MARKERS["slice_dry_run"] == SLICE_NA
    assert GATE_NA_MARKERS["region_containment"] == REGION_NA
