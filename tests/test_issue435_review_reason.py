"""tests/test_issue435_review_reason.py — the review reason is not a post-check.

Issue #435 (lens round 2): ``visual_review_failed`` is a loop-level terminal
reason. It must stay out of ``POST_CHECK_REASONS`` (the #432 identical-repair
stop and the attempts sentence key off that set) yet remain in the failure
archive's loop-level vocabulary.
"""

from __future__ import annotations

from d33d.evals.failure_capture import LOOP_LEVEL_FAILURE_REASONS
from d33d.post_check_reasons import POST_CHECK_REASONS, VISUAL_REVIEW_FAILED_REASON


def test_visual_review_reason_is_not_a_post_check():
    assert VISUAL_REVIEW_FAILED_REASON not in POST_CHECK_REASONS


def test_visual_review_reason_is_a_loop_level_failure_reason():
    assert VISUAL_REVIEW_FAILED_REASON in LOOP_LEVEL_FAILURE_REASONS
