"""11-class OpenSCAD LLM failure classifier with repair routing.

Part of the design-agent design loop (issue #5, workstream
task-failures). The LLM emits parametric OpenSCAD that the render worker
compiles and renders; when the render fails (or the geometry is wrong),
this module classifies the failure into one of the 11 named classes and
routes it back to the LLM as structured repair input.

The 11 classes are the closed enum for OpenSCAD LLM failure modes —
distinct from the 7-class render-worker ``ErrorClass`` (ok, syntax_error,
empty_model, artifact_error, timeout, oom, container_error) which
classifies *render run* outcomes. This module classifies *LLM output*
failure modes, which are the ones the repair loop can act on.

Classification strategy
------------------------

Two layers:

1. **Deterministic stderr-based classification** (``classify_failure``):
   called when the render worker returns a non-``ok`` ``error_class``.
   Uses regex patterns over the OpenSCAD diagnostic output to identify
   the specific failure class. First-match-wins; the fallback is
   ``unclassified_syntax_error`` for any unrecognised ``syntax_error``
   from the render worker, or the render-worker class itself for
   non-syntax classes (timeout, oom, etc.) which are non-improving
   steps, not repair inputs.

2. **Vision-only class** (``geometrically_wrong``): when the render
   succeeds (``error_class == "ok"``) but the bounding-box gate or the
   vision critique flags a mismatch, this class is the only one the
   vision loop can catch. It is not detected by stderr patterns.

Repair routing
--------------

``route_repair`` takes a classified failure and returns a structured
``RepairDirective`` that the design loop feeds back to the LLM as
repair input. Each class carries a targeted repair instruction; the
directive is *structured* (class name + instruction), never a raw
stderr dump.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal

# ---------------------------------------------------------------------------
# Failure class enum
# ---------------------------------------------------------------------------

OpenSCADFailureClass = Literal[
    # 11 named LLM failure classes
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
    # Non-repairable render-worker classes
    "timeout",
    "oom",
    "container_error",
    "artifact_error",
    "empty_model",
    # Fallback for unrecognised syntax errors
    "unclassified_syntax_error",
]

#: Closed enum of all 17 entries: 11 named LLM failure classes, 5
#: non-repairable render-worker classes, and 1 fallback.
FAILURE_CLASSES: frozenset[OpenSCADFailureClass] = frozenset(
    {
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
        "timeout",
        "oom",
        "container_error",
        "artifact_error",
        "empty_model",
        "unclassified_syntax_error",
    }
)

#: The 12 classes that are repairable by the design loop.
#: ``unclassified_syntax_error`` is included because the LLM can fix
#: generic syntax errors even when the specific cause is unknown.
REPAIRABLE_CLASSES: frozenset[OpenSCADFailureClass] = frozenset(
    {
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
        "unclassified_syntax_error",
    }
)

#: The 5 non-repairable render-worker classes. These map to non-improving
#: steps in the design loop; they must NOT be routed back to the LLM as
#: repair input.
NON_REPAIRABLE_CLASSES: frozenset[OpenSCADFailureClass] = frozenset(
    {
        "timeout",
        "oom",
        "container_error",
        "artifact_error",
        "empty_model",
    }
)

# ---------------------------------------------------------------------------
# Regex patterns for deterministic stderr classification
# ---------------------------------------------------------------------------

# Hallucinated BOSL2 module or parameter name
_RE_HALLUCINATED_BOSL2 = re.compile(
    r"Unknown module|No such module|not a module|bosl2",
    re.IGNORECASE,
)

# text() with unavailable font — matches font= or Font "..."
_RE_TEXT_FONT = re.compile(
    r"font\s*[=\"']",
    re.IGNORECASE,
)

# hull() or minkowski() misuse, including nested-boolean bug #1027
_RE_HULL_MINKOWSKI = re.compile(
    r"(hull|minkowski)\s*\(",
    re.IGNORECASE,
)

# difference() operand inversion
_RE_DIFFERENCE_INVERSION = re.compile(
    r"difference\s*\(",
    re.IGNORECASE,
)

# projection() or offset() CGAL fragility
_RE_PROJECTION_OFFSET = re.compile(
    r"(projection|offset)\s*\(",
    re.IGNORECASE,
)

# Z-up vs Y-up confusion
_RE_ZUP_YUP = re.compile(
    r"(y-up|z-up|Blender|yup|zup)",
    re.IGNORECASE,
)

# Transform composition order
_RE_TRANSFORM_ORDER = re.compile(
    r"(translate|rotate|scale)\s*\([^)]*\)\s*(translate|rotate|scale)",
    re.IGNORECASE,
)

# Rotation about wrong axis
_RE_WRONG_AXIS = re.compile(
    r"rotate\s*\(\s*\d+\s*,\s*\[",
)

# Trailing semicolon after modifier (rare in stderr; mostly a vision issue)
_RE_TRAILING_SEMICOLON = re.compile(
    r"translate\s*\([^)]*\)\s*;\s*\w",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ClassifiedFailure:
    """A classified LLM output failure.

    ``failure_class`` is one of the 11 named LLM classes or one of the
    5 non-repairable render-worker classes or the fallback.
    ``repairable`` is True iff the class is in ``REPAIRABLE_CLASSES``.
    ``evidence`` is the stderr fragment or gate result that triggered
    the classification.
    """

    failure_class: OpenSCADFailureClass
    evidence: str
    repairable: bool

    @property
    def is_repairable(self) -> bool:
        return self.repairable


@dataclass(frozen=True)
class RepairDirective:
    """Structured repair input fed back to the LLM.

    ``failure_class`` identifies what went wrong. ``instruction`` is a
    targeted, imperative repair instruction for the LLM. ``scad_source``
    is the offending .scad (the LLM may want to see it). ``evidence`` is
    the stderr fragment or gate result.
    """

    failure_class: OpenSCADFailureClass
    instruction: str
    scad_source: str
    evidence: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "failure_class": self.failure_class,
            "instruction": self.instruction,
            "scad_source": self.scad_source,
            "evidence": self.evidence,
        }


# ---------------------------------------------------------------------------
# Repair instructions per class
# ---------------------------------------------------------------------------

_REPAIR_INSTRUCTIONS: dict[OpenSCADFailureClass, str] = {
    "trailing_semicolon": (
        "Remove the trailing semicolon after translate/rotate/scale modifiers. "
        "A semicolon terminates a modifier, leaving the next shape at the origin. "
        "Correct: translate([x,y,z]) cube(...)  (no semicolon after the modifier)."
    ),
    "transform_order": (
        "Check transform composition order. Transforms compose like matrix "
        "multiplication — order matters. If you want to rotate then translate, "
        "write rotate(...) translate(...) (rightmost applies first)."
    ),
    "wrong_axis_rotation": (
        "Check the rotation axis. rotate(angle, [x, y, z]) — verify the axis "
        "vector is correct. In OpenSCAD, Z is up, Y is into the screen. "
        "If you are thinking in Y-up (Blender), the axis is likely wrong."
    ),
    "difference_inversion": (
        "Check difference() operand order. difference() { A; B; } subtracts B from A. "
        "If the result is inverted (cutting the wrong shape), swap the operands."
    ),
    "hull_miskowski_misuse": (
        "Check hull() or minkowski() usage. hull() takes the convex hull of its "
        "children. minkowski() takes the Minkowski sum. Both require at least two "
        "children. Nested booleans inside hull/minkowski are a known bug (#1027)."
    ),
    "zup_yup_confusion": (
        "OpenSCAD uses Z-up (Z is the vertical axis), not Y-up (Blender). "
        "If the model appears rotated or flattened, check that you are using "
        "the correct vertical axis. Translate in Z for vertical movement."
    ),
    "magic_numbers": (
        "Replace magic numbers with named parameters. Every dimension value that "
        "appears in the .scad should be declared as a named variable in the "
        "parameter block at the top of the file. The user's stated dimensions "
        "must appear as named parameters, never as inline literals."
    ),
    "projection_offset_fragile": (
        "Check projection() and offset() usage. Both are CGAL-dependent and can "
        "be fragile with certain geometry. Ensure the geometry is valid before "
        "applying projection/offset, and consider simplifying the geometry first."
    ),
    "text_missing_font": (
        "The font specified in text() is not available in the render environment. "
        "Use a font that is available (e.g., 'DejaVu Sans', 'Arial', or the "
        "default font), or remove the font parameter to use the default."
    ),
    "hallucinated_bosl2": (
        "The BOSL2 module or parameter name is not valid. Use the BOSL2 cheatsheet "
        "provided in the system prompt to verify module and parameter names. "
        "If a module is not in the cheatsheet, it likely does not exist. "
        "Use confirmed BOSL2 modules: rounding, attach/align, hull, minkowski."
    ),
    "geometrically_wrong": (
        "The model compiles and renders successfully, but the geometry does not "
        "match the reference photo or the stated dimensions. Review the bounding "
        "box, shape, and features against the reference. This is a vision-critique "
        "failure — the code is syntactically correct but geometrically wrong."
    ),
    "unclassified_syntax_error": (
        "The render failed with a syntax error. Review the OpenSCAD source for "
        "syntax errors (mismatched braces, missing semicolons, invalid expressions). "
        "Fix the syntax and re-render."
    ),
    # Non-repairable classes — no repair instruction
    "timeout": "",
    "oom": "",
    "container_error": "",
    "artifact_error": "",
    "empty_model": "",
}


# ---------------------------------------------------------------------------
# Classification function
# ---------------------------------------------------------------------------

def _classify_syntax_error(stderr: str) -> OpenSCADFailureClass:
    """Classify a syntax_error by inspecting the stderr output for
    specific failure patterns.

    First-match-wins. If no pattern matches, falls back to
    ``unclassified_syntax_error``.
    """
    # Order matters: more specific patterns first.
    # 1. Hallucinated BOSL2 (most specific)
    if _RE_HALLUCINATED_BOSL2.search(stderr):
        return "hallucinated_bosl2"
    # 2. Text font missing
    if _RE_TEXT_FONT.search(stderr):
        return "text_missing_font"
    # 3. Hull/minkowski misuse
    if _RE_HULL_MINKOWSKI.search(stderr):
        return "hull_miskowski_misuse"
    # 4. Difference inversion
    if _RE_DIFFERENCE_INVERSION.search(stderr):
        return "difference_inversion"
    # 5. Projection/offset fragility
    if _RE_PROJECTION_OFFSET.search(stderr):
        return "projection_offset_fragile"
    # 6. Z-up/Y-up confusion
    if _RE_ZUP_YUP.search(stderr):
        return "zup_yup_confusion"
    # 7. Transform order
    if _RE_TRANSFORM_ORDER.search(stderr):
        return "transform_order"
    # 8. Wrong axis rotation
    if _RE_WRONG_AXIS.search(stderr):
        return "wrong_axis_rotation"
    # 9. Trailing semicolon (rare in stderr)
    if _RE_TRAILING_SEMICOLON.search(stderr):
        return "trailing_semicolon"
    # 10. Fallback
    return "unclassified_syntax_error"


def classify_failure(
    *,
    error_class: str,
    stderr: str = "",
    scad_source: str = "",
) -> ClassifiedFailure:
    """Classify a render run failure into a named OpenSCAD LLM failure class.

    Parameters
    ----------
    error_class:
        The render-worker ``ErrorClass`` (ok, syntax_error, empty_model,
        artifact_error, timeout, oom, container_error).
    stderr:
        The OpenSCAD diagnostic output (truncated to 256 KiB).
    scad_source:
        The .scad source code.

    Returns
    -------
    ClassifiedFailure:
        The classified failure with ``failure_class``, ``evidence``, and
        ``repairable`` fields.

    Rules:
    - ``ok`` → ``geometrically_wrong`` (vision-only class)
    - ``timeout`` / ``oom`` / ``container_error`` / ``artifact_error`` /
      ``empty_model`` → the corresponding non-repairable class
    - ``syntax_error`` → inspect stderr for specific patterns; first match
      wins; fallback is ``unclassified_syntax_error``
    """
    if error_class == "ok":
        return ClassifiedFailure(
            failure_class="geometrically_wrong",
            evidence="Render succeeded but vision critique flagged geometric mismatch",
            repairable=True,
        )

    if error_class in ("timeout", "oom", "container_error", "artifact_error", "empty_model"):
        return ClassifiedFailure(
            failure_class=error_class,  # type: ignore[arg-type]
            evidence=stderr[:256] if stderr else f"Render worker class: {error_class}",
            repairable=False,
        )

    # syntax_error — inspect stderr
    failure_class = _classify_syntax_error(stderr)
    return ClassifiedFailure(
        failure_class=failure_class,
        evidence=stderr[:256] if stderr else "syntax_error (no stderr)",
        repairable=True,
    )


def detect_magic_numbers(
    scad_source: str,
    stated_dimensions: dict[str, float] | None = None,
) -> bool:
    """Detect magic numbers in .scad source.

    Returns True if the .scad contains numeric literals (2+ digits) in
    geometric expressions that are not declared as named parameters.

    Single-digit numbers are not considered magic numbers — they are
    commonly used for small offsets, angles, and other non-dimension values.

    Parameters
    ----------
    scad_source:
        The .scad source code.
    stated_dimensions:
        Optional dict of stated dimensions. Not used for detection;
        the heuristic is based on inline literals.

    Returns
    -------
    bool:
        True if magic numbers (2+ digit inline literals in geometric
        expressions) were detected.
    """
    lines = scad_source.split("\n")
    # Collect declared parameter values
    param_declarations: set[str] = set()
    for line in lines:
        stripped = line.strip()
        match = re.match(r"^(\w+)\s*=\s*([\d.]+)\s*;", stripped)
        if match:
            param_declarations.add(match.group(2))

    for line in lines:
        stripped = line.strip()
        if stripped.startswith(("//", "/*")):
            continue
        # Find numeric literals (2+ digits) in geometric function calls
        for match in re.finditer(
            r"(cube|cylinder|sphere|square|polygon|translate|rotate)\s*\(\s*\[?\s*(\d{2,}(?:\.\d+)?)",
            stripped,
        ):
            value_str = match.group(2)
            if value_str not in param_declarations:
                return True
    return False


# ---------------------------------------------------------------------------
# Repair routing
# ---------------------------------------------------------------------------

def route_repair(
    *,
    classified: ClassifiedFailure,
    scad_source: str,
) -> RepairDirective | None:
    """Route a classified failure to a structured repair directive.

    Returns ``None`` for non-repairable classes (timeout, oom, etc.) —
    these are non-improving steps and must NOT be fed back to the LLM.

    Returns a ``RepairDirective`` for repairable classes, containing the
    failure class, a targeted repair instruction, the offending .scad,
    and the evidence.
    """
    if not classified.repairable:
        return None

    instruction = _REPAIR_INSTRUCTIONS.get(classified.failure_class, "")
    if not instruction:
        # Fallback: generic syntax error instruction
        instruction = _REPAIR_INSTRUCTIONS["unclassified_syntax_error"]

    return RepairDirective(
        failure_class=classified.failure_class,
        instruction=instruction,
        scad_source=scad_source,
        evidence=classified.evidence,
    )
