"""tests/test_issue435_review_parse.py — the review parse never aborts the run.

Issue #435 (lens round 2): ``parse_review`` runs on whatever the role edge
returned. A malformed reply object (no ``content`` / ``tool_calls``) must map to
``unreviewed("unparseable")``, never raise out of ``review_render``.
"""

from __future__ import annotations

import asyncio
import logging

from d33d.visual_review import REQUIRED_VIEW_NAMES, ReviewVerdict, review_render

_VIEWS = {name: "data:image/png;base64,AAAA" for name in REQUIRED_VIEW_NAMES}


def test_non_reply_object_is_unreviewed_unparseable(caplog):
    caplog.set_level(logging.WARNING, logger="d33d.visual_review")

    def llm_fn(role, messages, system):
        return object()

    verdict = asyncio.run(review_render(_VIEWS, "a bracket", llm_fn=llm_fn))
    assert verdict == ReviewVerdict.unreviewed("unparseable")
    assert any("unparseable" in r.getMessage() for r in caplog.records)
