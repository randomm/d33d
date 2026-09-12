"""Tests for hash-pinned prompt files (issue #9, workstream task-golden-set).

Covers:
- (a) every golden case references a prompt file by hash, not a mutable path
- (b) prompt content drift (file edited, pin not updated) is a hard failure
- (c) the regression attribution is readable: prompt-version per case
- (d) swapping a prompt file and re-pinning keeps the suite green
- (e) prompt_file_hash is content-based, not path-based
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from d33d.evals.case_schema import (
    GoldenCase,
    PromptPin,
    check_prompt_pin,
    load_golden_set,
    prompt_file_hash,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
CASES_DIR = REPO_ROOT / "evals" / "cases"
PROMPTS_DIR = REPO_ROOT / "evals" / "prompts"


def _load() -> dict[str, GoldenCase]:
    return load_golden_set(CASES_DIR, REPO_ROOT)


# ---------------------------------------------------------------------------
# (a) every case references a prompt by hash
# ---------------------------------------------------------------------------


def test_every_case_pins_a_prompt_by_hash() -> None:
    for cid, c in _load().items():
        assert c.prompt.sha256, f"{cid}: no sha256 on prompt pin"
        assert len(c.prompt.sha256) == 64, f"{cid}: sha256 must be 64 hex chars"
        int(c.prompt.sha256, 16)  # must be valid hex


def test_every_prompt_pin_path_points_to_a_real_file() -> None:
    for cid, c in _load().items():
        full = REPO_ROOT / c.prompt.path
        assert full.is_file(), f"{cid}: prompt path {c.prompt.path!r} does not exist"


def test_prompt_pin_hash_matches_file_content() -> None:
    """The pin is over the file content — a re-hash must match exactly."""
    for cid, c in _load().items():
        actual = hashlib.sha256((REPO_ROOT / c.prompt.path).read_bytes()).hexdigest()
        assert actual == c.prompt.sha256, (
            f"{cid}: pin {c.prompt.sha256[:12]}… != content {actual[:12]}… "
            f"(prompt drift — re-pin or restore)"
        )


# ---------------------------------------------------------------------------
# (b) drift detection: content changed, pin not updated → hard failure
# ---------------------------------------------------------------------------


def test_prompt_drift_is_detected(tmp_path: Path) -> None:
    """If the prompt file content changes but the pin hash doesn't, the
    check raises — a regression, not a silent pass."""
    prompts = tmp_path / "prompts"
    prompts.mkdir()
    original = b"# prompt v1\noriginal content\n"
    (prompts / "p.md").write_bytes(original)
    good = hashlib.sha256(original).hexdigest()

    pin = PromptPin(prompt_version="v1", path="prompts/p.md", sha256=good)
    assert check_prompt_pin(tmp_path, pin) == good

    # Now the file content changes — the pin is stale.
    (prompts / "p.md").write_bytes(b"# prompt v1\nCHANGED content\n")
    with pytest.raises(ValueError, match="prompt drift"):
        check_prompt_pin(tmp_path, pin)


def test_prompt_drift_on_a_real_case(tmp_path: Path) -> None:
    """Copy a real case + its prompt to a sandbox, mutate the prompt, and
    confirm load_golden_set fails (drift is a hard error)."""
    cases_src = CASES_DIR
    src_case = next(cases_src.glob("*.json"))
    case = json.loads(src_case.read_text())
    pin = case["prompt"]

    prompts_dst = tmp_path / "evals" / "prompts"
    prompts_dst.mkdir(parents=True)
    dst_prompt = prompts_dst / Path(pin["path"]).name
    dst_prompt.write_bytes((REPO_ROOT / pin["path"]).read_bytes())

    cases_dst = tmp_path / "evals" / "cases"
    cases_dst.mkdir(parents=True)
    # rewrite the pin path to the sandbox prompt
    case["prompt"]["path"] = str(dst_prompt.relative_to(tmp_path))
    (cases_dst / src_case.name).write_text(json.dumps(case))

    # happy path: no drift
    loaded = load_golden_set(cases_dst, tmp_path)
    assert src_case.stem in loaded

    # drift: mutate the prompt content
    dst_prompt.write_bytes(dst_prompt.read_bytes() + b" changed\n")
    with pytest.raises(ValueError, match="prompt drift"):
        load_golden_set(cases_dst, tmp_path)


def test_missing_prompt_file_is_an_error(tmp_path: Path) -> None:
    pin = PromptPin(prompt_version="v1", path="prompts/gone.md", sha256="0" * 64)
    with pytest.raises(FileNotFoundError):
        check_prompt_pin(tmp_path, pin)


# ---------------------------------------------------------------------------
# (c) regression attribution: "prompt v7 fails case 12 which v5 passed"
# ---------------------------------------------------------------------------


def test_prompt_version_is_attribution_per_case() -> None:
    """Each case carries a prompt_version — so a report line reads
    'prompt v7 fails case X' without any other lookup."""
    for c in _load().values():
        # version tag is present, well-formed, and distinct from the hash
        assert c.prompt.prompt_version != c.prompt.sha256
        assert c.prompt.prompt_version.startswith("v")
        assert any(ch.isdigit() for ch in c.prompt.prompt_version)


def test_two_cases_can_share_a_prompt_pin() -> None:
    """Cases that share a prompt version share the same hash — the
    attribution groups by prompt version."""
    cases = _load()
    versions = {c.prompt.prompt_version for c in cases.values()}
    assert versions, "no cases loaded"
    # at least one version is shared by multiple cases (the seed uses
    # one prompt per kind, so this holds by construction)
    from collections import Counter

    counts = Counter(c.prompt.prompt_version for c in cases.values())
    assert any(n > 1 for n in counts.values()), "expected a shared prompt version"


# ---------------------------------------------------------------------------
# (d) swapping a prompt file + re-pinning keeps the suite green
# ---------------------------------------------------------------------------


def test_repin_after_prompt_swap_passes(tmp_path: Path) -> None:
    """Swap the prompt content to a new version and update the pin to the
    new hash — the check passes again (the regression is resolved)."""
    prompts = tmp_path / "prompts"
    prompts.mkdir()
    v1 = b"# prompt v1\noriginal\n"
    (prompts / "p.md").write_bytes(v1)
    pin_v1 = PromptPin(prompt_version="v1", path="prompts/p.md", sha256=hashlib.sha256(v1).hexdigest())
    assert check_prompt_pin(tmp_path, pin_v1)

    v2 = b"# prompt v2\nrevised content\n"
    (prompts / "p.md").write_bytes(v2)
    new_hash = hashlib.sha256(v2).hexdigest()
    pin_v2 = PromptPin(prompt_version="v2", path="prompts/p.md", sha256=new_hash)
    assert check_prompt_pin(tmp_path, pin_v2) == new_hash


def test_rename_keeps_pin_valid(tmp_path: Path) -> None:
    """The pin is content-based: renaming the file keeps the pin valid
    (the path is metadata; the hash is the pin)."""
    prompts = tmp_path / "prompts"
    prompts.mkdir()
    content = b"# prompt v1\nstable content\n"
    (prompts / "old_name.md").write_bytes(content)
    h = hashlib.sha256(content).hexdigest()

    # rename the file
    (prompts / "old_name.md").rename(prompts / "new_name.md")

    # pin pointing at the NEW path, same content → valid
    pin = PromptPin(prompt_version="v1", path="prompts/new_name.md", sha256=h)
    assert check_prompt_pin(tmp_path, pin) == h


# ---------------------------------------------------------------------------
# (e) prompt_file_hash is content-based
# ---------------------------------------------------------------------------


def test_prompt_file_hash_is_content_not_path(tmp_path: Path) -> None:
    a = tmp_path / "a.md"
    b = tmp_path / "b.md"
    content = b"identical bytes\n"
    a.write_bytes(content)
    b.write_bytes(content)
    ha = prompt_file_hash(tmp_path, "a.md")
    hb = prompt_file_hash(tmp_path, "b.md")
    assert ha == hb
    assert ha == hashlib.sha256(content).hexdigest()


def test_prompt_file_hash_differs_on_content_diff(tmp_path: Path) -> None:
    a = tmp_path / "a.md"
    b = tmp_path / "b.md"
    a.write_bytes(b"one\n")
    b.write_bytes(b"two\n")
    assert prompt_file_hash(tmp_path, "a.md") != prompt_file_hash(tmp_path, "b.md")


def test_prompt_file_hash_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        prompt_file_hash(tmp_path, "missing.md")
