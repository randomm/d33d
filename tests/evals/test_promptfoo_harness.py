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
    assert len(prompt_files) == 5, f"expected 5 prompt files, got {len(prompt_files)}"

    # Every case's pin path points into the config's prompts_dir.
    cases = load_golden_set(REPO_ROOT / doc["cases_dir"], REPO_ROOT)
    for case in cases.values():
        pin_path = REPO_ROOT / case.prompt.path
        assert pin_path.is_file()
        assert prompts_dir in pin_path.parents or pin_path.parent == prompts_dir


def test_config_python_asserts_point_at_run_py_with_gates_first() -> None:
    """The config's custom asserts are python asserts through
    ``evals/run.py`` with the deterministic-gates assert present — the
    7 gates run before the judge."""
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
# Local-run fixture bounds (evals/run.py --run path)
# ---------------------------------------------------------------------------


class _CaseRef:
    """Duck of the case's reference fields for the local-render input path."""

    def __init__(self, reference_photo: str | None = None,
                 rendered_views: tuple[str, ...] = ()) -> None:
        self.reference_photo = reference_photo
        self.rendered_views = rendered_views


def test_local_render_inputs_rejects_path_outside_evals(tmp_path) -> None:
    """A case fixture that resolves OUTSIDE ``evals/`` (symlink-escape) is
    skipped, not read — the resolved path must stay inside ``evals/``."""
    run = _load_run_module()
    repo_root = tmp_path / "repo"
    (repo_root / "evals").mkdir(parents=True)
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"x")
    case = _CaseRef(reference_photo=str(outside))
    _render, stl, mesh = run._local_render_inputs(repo_root, case)
    assert stl is None
    assert mesh is None


def test_local_render_inputs_rejects_overlarge_image_fixture(tmp_path) -> None:
    """An image fixture over the 10 MB cap is skipped (never uploaded to
    the LLM) — the size bound is a cost/volume guard, not a crash.
    Only image fixtures (non-``.scad``) are subject to the image cap."""
    run = _load_run_module()
    repo_root = tmp_path / "repo"
    evals_dir = repo_root / "evals"
    evals_dir.mkdir(parents=True)
    big = evals_dir / "big.png"
    # Write just over the 10 MB cap in one block (the content is never
    # read — the size check skips before any processing).
    big.write_bytes(b"x" * (run.MAX_FIXTURE_IMAGE_BYTES + 1))
    case = _CaseRef(reference_photo=str(big))
    _render, stl, mesh = run._local_render_inputs(repo_root, case)
    assert stl is None
    assert mesh is None


def test_scad_to_mesh_rejects_path_outside_repo(tmp_path) -> None:
    """A ``.scad`` fixture that resolves OUTSIDE the repo (symlink-escape)
    is never handed to openscad — ``None`` (no mesh) is returned."""
    run = _load_run_module()
    repo_root = tmp_path / "repo"
    repo_root.mkdir(parents=True)
    outside = tmp_path / "outside.scad"
    outside.write_text("cube([1, 1, 1]);\n", encoding="utf-8")
    mesh = run._scad_to_mesh(repo_root, outside)
    assert mesh is None


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
