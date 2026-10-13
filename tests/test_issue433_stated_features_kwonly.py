"""Issue #433: ``stated_features`` is keyword-only on both design-loop entry
points, so a positional caller can never shift it into another parameter."""

from __future__ import annotations

import inspect

import pytest

from d33d.design_loop import run_design_loop, run_design_loop_async


@pytest.mark.parametrize("fn", [run_design_loop, run_design_loop_async])
def test_stated_features_is_keyword_only(fn) -> None:
    param = inspect.signature(fn).parameters["stated_features"]
    assert param.kind is inspect.Parameter.KEYWORD_ONLY
