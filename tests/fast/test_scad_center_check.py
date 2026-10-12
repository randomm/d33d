"""Issue #436: the wrong-typed ``center=[...]`` SCAD-text check."""

from __future__ import annotations

from pathlib import Path

import pytest

from d33d.failure_classes import ClassifiedFailure, route_repair
from d33d.scad_center_check import center_vector_warning

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "scad"


def test_vector_center_fixture_is_flagged() -> None:
    src = (FIXTURES / "groove-center-vector.scad").read_text(encoding="utf-8")
    finding = center_vector_warning(src)
    assert finding is not None
    call, evidence = finding
    assert "cube" in call
    assert "center" in evidence


def test_boolean_center_fixture_passes() -> None:
    src = (FIXTURES / "groove-center-true.scad").read_text(encoding="utf-8")
    assert center_vector_warning(src) is None


@pytest.mark.parametrize(
    "scad",
    [
        "cube([1, 2, 3], center = true);",
        "cube([1, 2, 3], center = false);",
        "size = [1, 2, 3];\ncube(size, center = some_var);",
        "cylinder(h = 5, r = 1, center = true);",
    ],
)
def test_boolean_or_variable_center_passes(scad: str) -> None:
    assert center_vector_warning(scad) is None


def test_comment_and_string_mentions_are_ignored() -> None:
    scad = (
        '// cube(10, center = [true, true, true]);\n'
        '/* cylinder(h = 1, center = [1, 2]); */\n'
        'echo("cube(center = [true])");\n'
        "cube(5, center = true);\n"
    )
    assert center_vector_warning(scad) is None


def test_vector_center_on_multiline_call_is_flagged() -> None:
    scad = "cube(\n  [4, 4, 4],\n  center = [true, true, true]\n);\n"
    assert center_vector_warning(scad) is not None


def test_routes_as_unknown_variable_naming_center() -> None:
    src = (FIXTURES / "groove-center-vector.scad").read_text(encoding="utf-8")
    finding = center_vector_warning(src)
    assert finding is not None
    _call, evidence = finding
    directive = route_repair(
        classified=ClassifiedFailure(
            failure_class="unknown_variable", evidence=evidence, repairable=True
        ),
        scad_source=src,
    )
    assert directive is not None
    assert directive.failure_class == "unknown_variable"
    assert "center" in directive.evidence
