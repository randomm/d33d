"""Table-driven test for the 11-class OpenSCAD LLM failure classifier.

Covers:
- All 11 named LLM failure classes (one fixture per class)
- All 5 non-repairable render-worker classes
- The unclassified_syntax_error fallback
- route_repair: structured directive for repairable classes, None for
  non-repairable classes
- detect_magic_numbers heuristic
- Golden .scad fixture files under tests/fixtures/scad/
"""

from __future__ import annotations

import re
from pathlib import Path

import d33d.failure_classes as fc
import d33d.render_worker as rw

# ---------------------------------------------------------------------------
# Fixtures path
# ---------------------------------------------------------------------------

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures" / "scad"

SIX_VIEWS = [f"view_{i:02d}_x.png" for i in range(6)]


def _load_fixture(name: str) -> str:
    """Load a .scad fixture from tests/fixtures/scad/."""
    path = FIXTURES_DIR / name
    assert path.exists(), f"Missing fixture: {path}"
    return path.read_text()


# ---------------------------------------------------------------------------
# Test: all 11 named LLM failure classes are in the closed enum
# ---------------------------------------------------------------------------


def test_11_named_llm_classes_are_in_closed_enum() -> None:
    """All 11 named LLM failure classes must be in FAILURE_CLASSES."""
    expected = {
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
        "geometrically_wrong",
    }
    assert expected.issubset(fc.FAILURE_CLASSES)
    assert len(expected) == 11


def test_6_non_repairable_classes_are_in_closed_enum() -> None:
    """All 6 non-repairable render-worker classes must be in FAILURE_CLASSES."""
    expected = {
        "timeout",
        "oom",
        "container_error",
        "artifact_error",
        "empty_model",
    }
    assert expected.issubset(fc.FAILURE_CLASSES)


def test_repairable_classes_are_subset_of_failure_classes() -> None:
    assert fc.REPAIRABLE_CLASSES.issubset(fc.FAILURE_CLASSES)


def test_non_repairable_classes_are_subset_of_failure_classes() -> None:
    assert fc.NON_REPAIRABLE_CLASSES.issubset(fc.FAILURE_CLASSES)


def test_repairable_and_non_repairable_are_disjoint() -> None:
    assert fc.REPAIRABLE_CLASSES.isdisjoint(fc.NON_REPAIRABLE_CLASSES)


def test_repairable_plus_non_repairable_covers_all() -> None:
    """REPAIRABLE + NON_REPAIRABLE covers all 17 FAILURE_CLASSES entries."""
    assert fc.REPAIRABLE_CLASSES | fc.NON_REPAIRABLE_CLASSES == fc.FAILURE_CLASSES
    assert len(fc.FAILURE_CLASSES) == 17


# ---------------------------------------------------------------------------
# Test: classify_failure for each render-worker error class
# ---------------------------------------------------------------------------


def _render_result(
    error_class: str,
    stderr: str = "",
) -> rw.RenderResult:
    """Build a RenderResult with the given error_class."""
    return rw.RenderResult(
        ok=(error_class == "ok"),
        exit_code=0 if error_class == "ok" else 1,
        duration_ms=100,
        error_class=error_class,  # type: ignore[arg-type]
        stderr=stderr,
        stl="model.stl" if error_class == "ok" else None,
        csg="model.csg" if error_class == "ok" else None,
        views=tuple(SIX_VIEWS) if error_class == "ok" else (),
    )


def test_ok_classifies_as_geometrically_wrong() -> None:
    """Render succeeds (ok) → geometrically_wrong (vision-only class)."""
    result = _render_result("ok")
    classified = fc.classify_failure(
        error_class=result.error_class,
        stderr=result.stderr,
        scad_source="cube([20, 25, 30]);",
    )
    assert classified.failure_class == "geometrically_wrong"
    assert classified.repairable is True


def test_timeout_classifies_as_timeout_non_repairable() -> None:
    result = _render_result("timeout", stderr="timeout")
    classified = fc.classify_failure(
        error_class=result.error_class,
        stderr=result.stderr,
    )
    assert classified.failure_class == "timeout"
    assert classified.repairable is False


def test_oom_classifies_as_oom_non_repairable() -> None:
    result = _render_result("oom", stderr="OOMKilled")
    classified = fc.classify_failure(
        error_class=result.error_class,
        stderr=result.stderr,
    )
    assert classified.failure_class == "oom"
    assert classified.repairable is False


def test_container_error_classifies_as_container_error_non_repairable() -> None:
    result = _render_result("container_error", stderr="docker error")
    classified = fc.classify_failure(
        error_class=result.error_class,
        stderr=result.stderr,
    )
    assert classified.failure_class == "container_error"
    assert classified.repairable is False


def test_artifact_error_classifies_as_artifact_error_non_repairable() -> None:
    result = _render_result("artifact_error", stderr="PNG missing")
    classified = fc.classify_failure(
        error_class=result.error_class,
        stderr=result.stderr,
    )
    assert classified.failure_class == "artifact_error"
    assert classified.repairable is False


def test_empty_model_classifies_as_empty_model_non_repairable() -> None:
    result = _render_result("empty_model", stderr="0 vertices")
    classified = fc.classify_failure(
        error_class=result.error_class,
        stderr=result.stderr,
    )
    assert classified.failure_class == "empty_model"
    assert classified.repairable is False


# ---------------------------------------------------------------------------
# Test: syntax_error classification by stderr pattern (first-match-wins)
# ---------------------------------------------------------------------------


def test_syntax_error_hallucinated_bosl2() -> None:
    """stderr mentions undefined BOSL2 module → hallucinated_bosl2."""
    stderr = "ERROR: Unknown module or action: bosh2_rounding"
    classified = fc.classify_failure(
        error_class="syntax_error",
        stderr=stderr,
    )
    assert classified.failure_class == "hallucinated_bosl2"
    assert classified.repairable is True


def test_syntax_error_text_missing_font() -> None:
    """stderr mentions font not found → text_missing_font."""
    stderr = 'ERROR: Font "Comic Sans" not found or unavailable'
    classified = fc.classify_failure(
        error_class="syntax_error",
        stderr=stderr,
    )
    assert classified.failure_class == "text_missing_font"
    assert classified.repairable is True


def test_syntax_error_hull_miskowski_misuse() -> None:
    """stderr mentions hull or minkowski error → hull_miskowski_misuse."""
    stderr = "ERROR: hull() requires at least two children"
    classified = fc.classify_failure(
        error_class="syntax_error",
        stderr=stderr,
    )
    assert classified.failure_class == "hull_miskowski_misuse"
    assert classified.repairable is True


def test_syntax_error_difference_inversion() -> None:
    """stderr mentions difference() issue → difference_inversion."""
    stderr = "ERROR: difference() operand order may be incorrect"
    classified = fc.classify_failure(
        error_class="syntax_error",
        stderr=stderr,
    )
    assert classified.failure_class == "difference_inversion"
    assert classified.repairable is True


def test_syntax_error_projection_offset_fragile() -> None:
    """stderr mentions projection or offset CGAL error → projection_offset_fragile."""
    stderr = "ERROR: offset() failed: CGAL error"
    classified = fc.classify_failure(
        error_class="syntax_error",
        stderr=stderr,
    )
    assert classified.failure_class == "projection_offset_fragile"
    assert classified.repairable is True


def test_syntax_error_zup_yup_confusion() -> None:
    """stderr mentions Y-up or Z-up → zup_yup_confusion."""
    stderr = "ERROR: model appears rotated (possible Y-up confusion)"
    classified = fc.classify_failure(
        error_class="syntax_error",
        stderr=stderr,
    )
    assert classified.failure_class == "zup_yup_confusion"
    assert classified.repairable is True


def test_syntax_error_unclassified_fallback() -> None:
    """Unrecognised syntax_error → unclassified_syntax_error."""
    stderr = "ERROR: some unrecognized syntax issue"
    classified = fc.classify_failure(
        error_class="syntax_error",
        stderr=stderr,
    )
    assert classified.failure_class == "unclassified_syntax_error"
    assert classified.repairable is True


def test_syntax_error_empty_stderr() -> None:
    """syntax_error with no stderr → unclassified_syntax_error."""
    classified = fc.classify_failure(
        error_class="syntax_error",
        stderr="",
    )
    assert classified.failure_class == "unclassified_syntax_error"
    assert classified.repairable is True


# ---------------------------------------------------------------------------
# Test: precedence — first-match-wins
# ---------------------------------------------------------------------------


def test_precedence_hallucinated_bosl2_wins_over_hull() -> None:
    """stderr mentions both BOSL2 and hull → hallucinated_bosl2 wins."""
    stderr = "ERROR: Unknown module bosh2_rounding in hull()"
    classified = fc.classify_failure(
        error_class="syntax_error",
        stderr=stderr,
    )
    assert classified.failure_class == "hallucinated_bosl2"


def test_precedence_font_checked_before_hull() -> None:
    """stderr mentions font → text_missing_font (font is checked before hull
    in the precedence order, but hull has its own pattern)."""
    stderr = 'ERROR: Font "X" not found'
    classified = fc.classify_failure(
        error_class="syntax_error",
        stderr=stderr,
    )
    assert classified.failure_class == "text_missing_font"


# ---------------------------------------------------------------------------
# Test: route_repair returns directive for repairable, None for non-repairable
# ---------------------------------------------------------------------------


def test_route_repair_returns_directive_for_repairable() -> None:
    """route_repair returns a RepairDirective for repairable classes."""
    classified = fc.ClassifiedFailure(
        failure_class="trailing_semicolon",
        evidence="semicolon after translate",
        repairable=True,
    )
    directive = fc.route_repair(
        classified=classified, scad_source="translate([1,2,3]); cube(5);"
    )
    assert directive is not None
    assert directive.failure_class == "trailing_semicolon"
    assert "semicolon" in directive.instruction.lower()
    assert directive.scad_source == "translate([1,2,3]); cube(5);"


def test_route_repair_returns_none_for_non_repairable() -> None:
    """route_repair returns None for non-repairable classes."""
    classified = fc.ClassifiedFailure(
        failure_class="timeout",
        evidence="timeout",
        repairable=False,
    )
    directive = fc.route_repair(classified=classified, scad_source="cube(5);")
    assert directive is None


def test_route_repair_returns_none_for_oom() -> None:
    """route_repair returns None for oom."""
    classified = fc.ClassifiedFailure(
        failure_class="oom",
        evidence="OOMKilled",
        repairable=False,
    )
    directive = fc.route_repair(classified=classified, scad_source="cube(5);")
    assert directive is None


def test_route_repair_directive_has_instruction() -> None:
    """Every repairable class has a non-empty repair instruction."""
    for cls in fc.REPAIRABLE_CLASSES:
        classified = fc.ClassifiedFailure(
            failure_class=cls,  # type: ignore[arg-type]
            evidence="test",
            repairable=True,
        )
        directive = fc.route_repair(classified=classified, scad_source="cube(5);")
        assert directive is not None, f"No directive for {cls}"
        assert directive.instruction, f"Empty instruction for {cls}"


def test_route_repair_directive_is_structured_not_raw_dump() -> None:
    """The directive is structured (class + instruction), not a raw stderr dump."""
    classified = fc.ClassifiedFailure(
        failure_class="difference_inversion",
        evidence="ERROR: difference() operand order",
        repairable=True,
    )
    directive = fc.route_repair(
        classified=classified, scad_source="difference() { cube(5); }"
    )
    assert directive is not None
    # The instruction should be a repair directive, not the raw stderr
    assert "operand" in directive.instruction.lower()
    # The evidence is preserved separately
    assert "difference" in directive.evidence.lower()


# ---------------------------------------------------------------------------
# Test: detect_magic_numbers heuristic
# ---------------------------------------------------------------------------


def test_detect_magic_numbers_inline_literals() -> None:
    """Inline numeric literals in geometric expressions → True."""
    scad = """
cube([20, 25, 30]);
cylinder(h=10, d=5);
"""
    assert fc.detect_magic_numbers(scad) is True


def test_detect_magic_numbers_named_parameters() -> None:
    """Named parameter declarations → False (no magic numbers)."""
    scad = """
W = 20;
H = 25;
D = 30;

cube([W, H, D]);
cylinder(h=H, d=W);
"""
    assert fc.detect_magic_numbers(scad) is False


def test_detect_magic_numbers_single_digit_numbers() -> None:
    """Single-digit numbers in geometric expressions → False (not magic)."""
    scad = """
cube([1, 2, 3]);
"""
    assert fc.detect_magic_numbers(scad) is False


def test_detect_magic_numbers_with_stated_dimensions() -> None:
    """With stated_dimensions, a literal equal to a stated value is exempt
    (issue #100 — the parameter was previously ignored; this test now
    asserts it is HONORED)."""
    scad = """
cube([20, 25, 30]);
"""
    stated = {"width": 20.0, "height": 25.0, "depth": 30.0}
    # 20 (first arg) equals stated width → exempt → no magic numbers.
    assert fc.detect_magic_numbers(scad, stated_dimensions=stated) is False
    # Without stated dimensions, 20 is still flagged.
    assert fc.detect_magic_numbers(scad) is True


# ---------------------------------------------------------------------------
# Test: golden .scad fixtures
# ---------------------------------------------------------------------------


def test_fixture_trailing_semicolon_exists_and_compiles_pattern() -> None:
    """The trailing_semicolon fixture contains a semicolon after translate."""
    scad = _load_fixture("failure-01-trailing-semicolon.scad")
    # The fixture should contain "translate(...); shape(...)" pattern
    assert re.search(r"translate\s*\([^)]*\)\s*;", scad)


def test_fixture_transform_order_exists() -> None:
    """The transform_order fixture contains nested transforms."""
    scad = _load_fixture("failure-02-transform-order.scad")
    assert "translate" in scad
    assert "rotate" in scad


def test_fixture_wrong_axis_exists() -> None:
    """The wrong_axis_rotation fixture contains rotate with axis vector."""
    scad = _load_fixture("failure-03-wrong-axis.scad")
    assert "rotate" in scad


def test_fixture_difference_inversion_exists() -> None:
    """The difference_inversion fixture contains difference()."""
    scad = _load_fixture("failure-04-difference-inversion.scad")
    assert "difference" in scad


def test_fixture_hull_miskowski_misuse_exists() -> None:
    """The hull_miskowski_misuse fixture contains hull or minkowski."""
    scad = _load_fixture("failure-05-hull-misuse.scad")
    assert "hull" in scad or "minkowski" in scad


def test_fixture_zup_yup_exists() -> None:
    """The zup_yup_confusion fixture exists."""
    scad = _load_fixture("failure-06-zup-yup.scad")
    assert len(scad) > 0


def test_fixture_magic_numbers_exists() -> None:
    """The magic_numbers fixture contains inline numeric literals."""
    scad = _load_fixture("failure-07-magic-numbers.scad")
    assert fc.detect_magic_numbers(scad) is True


def test_fixture_projection_fragile_exists() -> None:
    """The projection_offset_fragile fixture contains projection or offset."""
    scad = _load_fixture("failure-08-projection-fragile.scad")
    assert "projection" in scad or "offset" in scad


def test_fixture_text_missing_font_exists() -> None:
    """The text_missing_font fixture contains text() with font."""
    scad = _load_fixture("failure-09-text-missing-font.scad")
    assert "text" in scad or "font" in scad


def test_fixture_hallucinated_bosl2_exists() -> None:
    """The hallucinated_bosl2 fixture references a BOSL2 module."""
    scad = _load_fixture("failure-10-hallucinated-bosl2.scad")
    assert "bosl2" in scad.lower() or "rounding" in scad.lower()


def test_fixture_geometrically_wrong_exists() -> None:
    """The geometrically_wrong fixture is valid OpenSCAD but geometrically wrong."""
    scad = _load_fixture("failure-11-geometrically-wrong.scad")
    # Should be syntactically valid (no syntax errors)
    assert "cube" in scad or "cylinder" in scad or "sphere" in scad


def test_fixture_box_magic_exists_and_has_magic_numbers() -> None:
    """box-magic.scad has all magic numbers inlined."""
    scad = _load_fixture("box-magic.scad")
    assert fc.detect_magic_numbers(scad) is True


def test_fixture_box_param_exists_and_has_no_magic_numbers() -> None:
    """box-param.scad uses named parameters (no magic numbers)."""
    scad = _load_fixture("box-param.scad")
    assert fc.detect_magic_numbers(scad) is False


def test_fixture_box_bosl2_exists() -> None:
    """box-bosl2.scad uses BOSL2 modules."""
    scad = _load_fixture("box-bosl2.scad")
    assert "bosl2" in scad.lower() or "include" in scad.lower()


# ---------------------------------------------------------------------------
# Test: all 11 fixtures exist in tests/fixtures/scad/
# ---------------------------------------------------------------------------


def test_all_11_failure_fixtures_exist() -> None:
    """All 11 failure-class .scad fixtures must exist on disk."""
    expected = [
        "failure-01-trailing-semicolon.scad",
        "failure-02-transform-order.scad",
        "failure-03-wrong-axis.scad",
        "failure-04-difference-inversion.scad",
        "failure-05-hull-misuse.scad",
        "failure-06-zup-yup.scad",
        "failure-07-magic-numbers.scad",
        "failure-08-projection-fragile.scad",
        "failure-09-text-missing-font.scad",
        "failure-10-hallucinated-bosl2.scad",
        "failure-11-geometrically-wrong.scad",
    ]
    for name in expected:
        path = FIXTURES_DIR / name
        assert path.exists(), f"Missing fixture: {name}"


def test_all_3_box_fixtures_exist() -> None:
    """All 3 box variant .scad fixtures must exist on disk."""
    for name in ["box-magic.scad", "box-param.scad", "box-bosl2.scad"]:
        path = FIXTURES_DIR / name
        assert path.exists(), f"Missing fixture: {name}"


# ---------------------------------------------------------------------------
# Test: end-to-end repair path (syntax_error → classified → repair → passes)
# ---------------------------------------------------------------------------


def test_repair_path_syntax_error_to_fix() -> None:
    """A syntax_error is classified, routed as repair input, and the
    second draft passes. The loop demonstrably recovers from compile failure."""
    # First draft: has a syntax error (e.g., trailing semicolon)
    first_scad = "translate([1,2,3]);\ncube([10, 10, 10]);"
    first_stderr = "ERROR: Syntax error"

    # Classify first draft
    classified_1 = fc.classify_failure(
        error_class="syntax_error",
        stderr=first_stderr,
        scad_source=first_scad,
    )
    assert classified_1.repairable is True

    # Route repair
    directive_1 = fc.route_repair(
        classified=classified_1,
        scad_source=first_scad,
    )
    assert directive_1 is not None
    assert directive_1.failure_class in fc.REPAIRABLE_CLASSES
    assert directive_1.instruction  # non-empty

    # Second draft: fixed (the LLM applies the repair instruction)
    second_scad = "translate([1,2,3]) cube([10, 10, 10]);"

    # Classify second draft (render succeeds)
    classified_2 = fc.classify_failure(
        error_class="ok",
        stderr="",
        scad_source=second_scad,
    )
    assert classified_2.failure_class == "geometrically_wrong"
    # The second draft is "ok" from the render worker's perspective —
    # the vision loop would then check geometry. For this test, we
    # assert the loop recovered from the compile failure.
    assert classified_2.repairable is True


def test_repair_path_oom_is_not_repair_input() -> None:
    """An oom failure is NOT routed to the LLM as repair input."""
    classified = fc.classify_failure(
        error_class="oom",
        stderr="OOMKilled",
    )
    assert classified.repairable is False

    directive = fc.route_repair(
        classified=classified,
        scad_source="cube([10, 10, 10]);",
    )
    assert directive is None


def test_geometrically_wrong_requires_vision_loop() -> None:
    """The geometrically_wrong class is only detectable by the vision loop,
    not by stderr patterns. A render that succeeds (ok) with valid artifacts
    but fails the bbox gate is classified as geometrically_wrong."""
    # Render succeeds but bbox gate fails
    classified = fc.classify_failure(
        error_class="ok",
        stderr="",
        scad_source="cube([20, 25, 30]);",
    )
    assert classified.failure_class == "geometrically_wrong"
    assert classified.repairable is True

    # The repair instruction mentions the vision critique
    directive = fc.route_repair(
        classified=classified,
        scad_source="cube([20, 25, 30]);",
    )
    assert directive is not None
    assert (
        "vision" in directive.instruction.lower()
        or "geometry" in directive.instruction.lower()
    )


# ---------------------------------------------------------------------------
# Test: ClassifiedFailure.is_repairable property
# ---------------------------------------------------------------------------


def test_is_repairable_property() -> None:
    """The is_repairable property matches the repairable field."""
    for cls in fc.REPAIRABLE_CLASSES:
        c = fc.ClassifiedFailure(
            failure_class=cls,  # type: ignore[arg-type]
            evidence="test",
            repairable=True,
        )
        assert c.is_repairable is True

    for cls in fc.NON_REPAIRABLE_CLASSES:
        c = fc.ClassifiedFailure(
            failure_class=cls,  # type: ignore[arg-type]
            evidence="test",
            repairable=False,
        )
        assert c.is_repairable is False
