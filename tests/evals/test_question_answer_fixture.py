"""The question-answer golden fixture (issue #249, workstream task-d).

The fixture (``evals/fixtures/question-answer/question-answer-height-12mm.json``)
pins a git-tracked question + design-state-block pair for the stage-2
number guard — Q "How tall is it now?" with block {W: 20 assumed,
D: 20 assumed, H: 12 stated}. It lives OUT of ``evals/cases/`` on purpose:
``load_golden_set`` validates every ``*.json`` under ``evals/cases/``
through the closed ``GoldenCase`` schema (5 design-loop kinds, >=1 gate
expectation), and the question-answer kind is a stage-2 harness fixture,
not a design-loop golden case, so the design-loop loader must never see
it.

Covers:
- the fixture file exists, is valid JSON, and keeps its filename in sync
  with its ``case_id``
- its prompt pin resolves to the hash-pinned question-answer prompt, and
  the pinned hash matches the on-disk content (drift detection)
- the ``expected`` shape the stage-2 number guard scores against:
  ``answerable``, ``answer_must_cite``, and the five-provenance phrase
  map (the merged closed set from #246/#248)
- the block carries exactly the stated/assumed provenance split the
  operator decision pins (H stated, W/D assumed)
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from d33d.evals.case_schema import prompt_file_hash

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE = REPO_ROOT / "evals" / "fixtures" / "question-answer" / "question-answer-height-12mm.json"

#: The five provenance phrases the stage-2 prompt maps the merged closed
#: provenance set to (operator decision for issue #249).
PROVENANCE_PHRASES = {
    "stated": "you said that",
    "measured": "I measured",
    "assumed": "I assumed",
    "unknown": "not established",
    "disagrees": "you said X, I measured Y",
}


def _fixture() -> dict:
    assert FIXTURE.is_file(), f"question-answer fixture missing: {FIXTURE}"
    return json.loads(FIXTURE.read_text())


def test_fixture_filename_matches_case_id() -> None:
    fixture = _fixture()
    assert fixture["case_id"] == FIXTURE.stem, (
        f"case_id {fixture['case_id']!r} != filename stem {FIXTURE.stem!r}"
    )


def test_fixture_pins_the_question_answer_prompt_by_hash() -> None:
    """The fixture's prompt pin points at the question-answer prompt and
    the pinned SHA-256 matches the on-disk content — an un-pinned prompt
    edit is a drift the test catches."""
    fixture = _fixture()
    pin = fixture["prompt"]
    assert pin["path"] == "evals/prompts/question_answer_v1.md"
    actual = prompt_file_hash(REPO_ROOT, pin["path"])
    assert actual == pin["sha256"], (
        f"prompt drift: {pin['path']} pinned {pin['sha256'][:12]}… "
        f"hashes to {actual[:12]}… (re-pin or restore)"
    )
    # sanity: the pin is a real 64-hex sha256, not a placeholder
    assert len(pin["sha256"]) == 64
    int(pin["sha256"], 16)
    # the fixture's expected phrases match the file content hash we verified
    assert hashlib.sha256(
        (REPO_ROOT / pin["path"]).read_bytes()
    ).hexdigest() == pin["sha256"]


def test_expected_shape_pins_answerable_cites_and_provenance_phrases() -> None:
    """The ``question_answer.expected`` shape is what the stage-2 number
    guard scores: an answerable flag, the numeric tokens the answer must
    cite, and the full five-provenance phrase map (the merged closed set
    from #246/#248 — the guard accepts exactly these phrases)."""
    fixture = _fixture()
    expected = fixture["question_answer"]["expected"]

    assert expected["answerable"] is True
    # H is the only stated axis: the answer must cite 12
    assert expected["answer_must_cite"] == ["12"]

    phrases = expected["provenance_phrases"]
    assert phrases == PROVENANCE_PHRASES
    # the five provenance keys are exactly the merged closed set
    assert set(phrases) == {"stated", "measured", "assumed", "unknown", "disagrees"}


def test_block_carries_stated_h_and_assumed_w_d() -> None:
    """The design-state block pins the operator decision's provenance
    split: H stated 12 mm (the operator said so), W/D assumed 20 mm —
    so the provenance-citing answer ('you said that' vs 'I assumed') is
    verifiable from the block alone."""
    fixture = _fixture()
    block = fixture["design_state_block"]

    assert block["H"] == {"value": 12, "unit": "mm", "provenance": "stated"}
    assert block["W"] == {"value": 20, "unit": "mm", "provenance": "assumed"}
    assert block["D"] == {"value": 20, "unit": "mm", "provenance": "assumed"}
    assert set(block) == {"W", "D", "H"}

    # the request is the exact live-bug repro question
    assert fixture["request"] == "How tall is it now?"


def test_fixture_is_outside_the_design_loop_golden_set_dir() -> None:
    """The fixture is NOT under ``evals/cases/`` — the design-loop
    ``load_golden_set`` validates every ``*.json`` there against the
    closed 5-kind ``GoldenCase`` schema, and the question-answer kind
    belongs to the stage-2 harness, not the design-loop golden set."""
    assert FIXTURE.parent == REPO_ROOT / "evals" / "fixtures" / "question-answer"
    assert REPO_ROOT / "evals" / "cases" not in FIXTURE.parents
