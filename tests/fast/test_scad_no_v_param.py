"""Fast guard: no ``v=`` argument in ``cube()``/``cylinder()``/``sphere()``
calls inside ``tests/slow/test_render_views.py`` or any
``tests/fixtures/scad/*.scad`` file (issue #269).

OpenSCAD has no ``v`` parameter on ``cube``/``cylinder``/``sphere``. It is
silently ignored (with a warning), so a misplaced ``v=`` silently produces
geometry at the origin instead of at the intended offset. The slow-test
marker model (``_marker_scad``) and the #234 framing fixtures both shipped
with this bug (issue #269), which made every asymmetric feature collapse to
the origin corner and defeated the per-view placement assertions.

This guard scans:
  1. The full text of ``tests/slow/test_render_views.py``.
  2. Every ``.scad`` file under ``tests/fixtures/scad/``.

and fails on any ``v=`` or ``v =`` argument inside a ``cube(``, ``cylinder(``,
or ``sphere(`` call, OR any vector ``center=[…]`` argument (issue #269
follow-up: OpenSCAD's ``center`` is a single boolean — a vector is silently
ignored, leaving the geometry uncentred, which is exactly the #269 bug
revisited). The scan is deliberately scoped to those three primitives
because the ``v`` parameter does not exist on them; other primitives (e.g.
``translate(``, ``rotate(``, ``scale(``) legitimately use other arguments.

Issue #269 also requires the ``_marker_scad()`` body itself to be a
``translate()``-based model (the five feature lines — post, short block,
+Y block, cone, difference-block — must place their geometry with
``translate`` rather than ``v=``). ``test_marker_scad_body_is_translate_based``
asserts that per-line, so a re-introduced ``v=`` on any of the five lines
fails the guard even if the ``v=`` scan above were narrowed.
"""

from __future__ import annotations

import re
from pathlib import Path

SLOW_TEST_FILE = (
    Path(__file__).resolve().parents[1] / "slow" / "test_render_views.py"
)
FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "scad"

# A ``v=`` or ``v =`` argument (with optional spaces around ``=``, and
# ensuring the preceding char is not part of a longer identifier, e.g.
# ``$v=`` or ``xv=`` would NOT match).
_V_ARG_RE = re.compile(r"\bv\s*=")

# A vector ``center=[…]`` argument (issue #269 follow-up: OpenSCAD's
# ``center`` is a single boolean; a vector is silently ignored, leaving
# the geometry uncentred — the exact bug the #269 fix was for).
_CENTER_VECTOR_RE = re.compile(r"\bcenter\s*=\s*\[")

# A line that contains any of the three primitives with an open paren.
_PRIM_LINE_RE = re.compile(r"\b(cube|cylinder|sphere)\s*\(")

# The five feature lines of ``_marker_scad()`` that issue #269 requires to
# be ``translate()``-based. Each entry is ``(feature name, a regex that must
# be found inside the ``_marker_scad`` body)``.
#
#   - post:          the tall +X post, ``translate([24, -4, 0])`` + ``cube([8, 8, 45])``
#   - short block:   the −X short block, ``translate([-32, -4, 0])`` + ``cube([8, 8, 22])``
#   - +Y block:      the +Y block, ``translate([-6, 22, 0])`` + ``cube([12, 12, 16])``
#   - cone:          the top-centre cone, ``cylinder(h=16, r1=8, r2=0, $fn=36)``
#   - difference-block: the difference-block (slab + notch cutter)
_MARKER_FEATURES: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "post",
        re.compile(r"translate\(\[24,\s*-4,\s*0\]\)\s*cube\(\[8,\s*8,\s*45\]\)"),
    ),
    (
        "short block",
        re.compile(r"translate\(\[-32,\s*-4,\s*0\]\)\s*cube\(\[8,\s*8,\s*22\]\)"),
    ),
    (
        "+Y block",
        re.compile(r"translate\(\[-6,\s*22,\s*0\]\)\s*cube\(\[12,\s*12,\s*16\]\)"),
    ),
    (
        "cone",
        re.compile(
            r"translate\(\[0,\s*0,\s*10\]\)\s*cylinder\(h\s*=\s*16,\s*"
            r"r1\s*=\s*8,\s*r2\s*=\s*0,\s*\$fn\s*=\s*36\)"
        ),
    ),
    (
        "difference-block",
        re.compile(r"difference\(\)\s*\{"),
    ),
)


def _has_v_arg(line: str) -> bool:
    """True if ``line`` contains a ``v=`` argument AND one of the three
    primitives (``cube``, ``cylinder``, ``sphere``) is called on that line.

    The ``\\bv\\s*=`` pattern matches ``v=`` at a word boundary (so it does
    NOT match ``xv=`` — the ``v`` in ``xv`` is not at the start of a word;
    in ``$v=`` the ``$`` is not a word char, so ``\\bv`` WOULD match the
    ``v`` after ``$``; to exclude ``$v`` we add a negative lookbehind).
    """
    # Exclude ``$v=`` (a variable named ``$v``) — not a real OpenSCAD case
    # but be precise.
    v_match = re.search(r"(?<!\$)\bv\s*=", line)
    if v_match is None:
        return False
    return _PRIM_LINE_RE.search(line) is not None


def _has_center_vector(line: str) -> bool:
    """True if ``line`` contains a vector ``center=[…]`` argument AND one
    of the three primitives (``cube``, ``cylinder``, ``sphere``) is called
    on that line (issue #269 follow-up: OpenSCAD's ``center`` is a single
    boolean; a vector is silently ignored, leaving the geometry uncentred).
    """
    if _CENTER_VECTOR_RE.search(line) is None:
        return False
    return _PRIM_LINE_RE.search(line) is not None


def _scan_source(text: str) -> list[tuple[int, str]]:
    """Return ``(line_no, line_text)`` pairs for every line in ``text``
    that contains a ``v=`` argument OR a vector ``center=[…]`` argument on
    a ``cube``/``cylinder``/``sphere`` call."""
    hits: list[tuple[int, str]] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        if _has_v_arg(line) or _has_center_vector(line):
            hits.append((lineno, line.rstrip()))
    return hits


def _marker_scad_body(text: str) -> str:
    """Return the source text of the ``_marker_scad()`` function body
    (from its ``return (`` line to the closing ``)`` before the next
    top-level ``def``/``class``/blank-line-then-``def`` boundary).

    The body is the SCAD string literal — the part of the file that the
    five feature-line assertions must inspect. The function's docstring
    (which may mention ``v=`` historically) is NOT part of the body.
    """
    start_marker = "def _marker_scad("
    start = text.find(start_marker)
    if start == -1:
        raise AssertionError(
            "cannot locate _marker_scad() in "
            f"{SLOW_TEST_FILE} — the guard cannot extract its body"
        )
    # Find the return statement's opening paren (the start of the SCAD
    # string literal).
    return_start = text.find("return (", start)
    if return_start == -1:
        raise AssertionError(
            "_marker_scad() has no `return (` statement — the guard "
            "cannot extract its body"
        )
    # Find the next top-level def/class after the return statement — that
    # is the end of the function.
    next_def = text.find("\ndef ", return_start)
    if next_def == -1:
        # End of file (shouldn't happen, but handle it).
        body = text[return_start:]
    else:
        body = text[return_start:next_def]
    # Strip the trailing `)` that closes the `return (` — we want the
    # string content, not the closing paren.
    return body


def test_no_v_arg_in_slow_test_file() -> None:
    """The full text of ``tests/slow/test_render_views.py`` must contain no
    ``v=`` argument AND no vector ``center=[…]`` argument on a
    ``cube``/``cylinder``/``sphere`` call (issue #269 + follow-up).

    The #234 framing fixtures at lines ~495/502 and the ``_marker_scad``
    body both previously used ``v=`` — the bug that made the marker model
    degenerate. The follow-up to #269 found the slab still used
    ``center=[true, true, false]`` — a vector OpenSCAD silently ignores
    (``center`` is a single boolean) — so the slab was still uncentred.
    The operator decision (issue #269 + follow-up) is that NO line may keep
    ``v=`` OR a vector ``center=[…]``: the slab must really be centred via
    ``translate``, and every feature and the notch cutter must use
    ``translate``.
    """
    text = SLOW_TEST_FILE.read_text(encoding="utf-8")
    hits = _scan_source(text)
    assert hits == [], (
        "v= or center=[…] argument found on a cube/cylinder/sphere call in "
        f"{SLOW_TEST_FILE} (issue #269: OpenSCAD has no v= parameter and "
        "center is a boolean, not a vector — use translate instead):\n"
        + "\n".join(f"  line {n}: {line}" for n, line in hits)
    )


def test_no_v_arg_in_scad_fixtures() -> None:
    """Every ``.scad`` file under ``tests/fixtures/scad/`` must contain no
    ``v=`` argument AND no vector ``center=[…]`` argument on a
    ``cube``/``cylinder``/``sphere`` call (issue #269 + follow-up).
    """
    assert FIXTURES_DIR.is_dir(), (
        f"fixtures dir missing: {FIXTURES_DIR}"
    )
    all_hits: list[tuple[str, int, str]] = []
    for scad in sorted(FIXTURES_DIR.glob("*.scad")):
        text = scad.read_text(encoding="utf-8")
        for lineno, line in _scan_source(text):
            all_hits.append((scad.name, lineno, line))
    assert all_hits == [], (
        "v= or center=[…] argument found on a cube/cylinder/sphere call in "
        "SCAD fixtures (issue #269: OpenSCAD has no v= parameter and center "
        "is a boolean, not a vector): \n"
        + "\n".join(f"  {name}:{n}: {line}" for name, n, line in all_hits)
    )


def test_marker_scad_body_is_translate_based() -> None:
    """The ``_marker_scad()`` body must be a ``translate()``-based model
    (issue #269): no ``v=`` argument on any of the five feature lines
    (post, short block, +Y block, cone, difference-block), and each of the
    four translate-features plus the difference-block must be present.

    This is the per-line guard that the file-wide ``v=`` scan above cannot
    express: it checks that the specific SCAD string literal in
    ``_marker_scad()`` contains the expected ``translate()`` calls (or,
    for the difference-block, the ``difference()`` construct) and that no
    ``v=`` argument appears anywhere in the body.
    """
    text = SLOW_TEST_FILE.read_text(encoding="utf-8")
    body = _marker_scad_body(text)

    # 1. No ``v=`` argument AND no vector ``center=[…]`` argument anywhere
    #    in the body (the file-wide scan above already covers the whole
    #    file, but this one is scoped to the body so it is precise about
    #    WHERE the violation would be).
    v_hits = [
        (lineno, line)
        for lineno, line in enumerate(body.splitlines(), start=1)
        if _has_v_arg(line) or _has_center_vector(line)
    ]
    assert v_hits == [], (
        "v= or center=[…] argument found in the _marker_scad() body "
        "(issue #269: OpenSCAD has no v= parameter and center is a "
        "boolean, not a vector — use translate instead):\n"
        + "\n".join(f"  body line {n}: {line}" for n, line in v_hits)
    )

    # 2. Each of the five feature lines must be present.
    missing = [
        name for name, pattern in _MARKER_FEATURES if not pattern.search(body)
    ]
    assert missing == [], (
        "expected feature(s) missing from the _marker_scad() body "
        f"(issue #269: the five feature lines must each be present in the "
        f"translate-based marker model): missing {missing!r}"
    )


def test_guard_detects_synthetic_v_arg() -> None:
    """The guard's helper must actually detect a ``v=`` argument — a
    self-test that the regex is not vacuous (issue #269)."""
    assert _has_v_arg("cube([8, 8, 45], v=[24, -4, 0]);") is True
    assert _has_v_arg("cylinder(h=16, r1=8, r2=0, v=[0,0,10]);") is True
    assert _has_v_arg("sphere([8], v=[0,0,10]);") is True
    # Not on a cube/cylinder/sphere line:
    assert _has_v_arg("translate([24, -4, 0]) cube([8, 8, 45]);") is False
    # $v= is not a real param; exclude it:
    assert _has_v_arg("cube([8, 8, 45], $v=[24, -4, 0]);") is False
    # v= on a different primitive:
    assert _has_v_arg("translate(v=[1,2,3]);") is False


def test_guard_detects_synthetic_center_vector() -> None:
    """The guard's helper must actually detect a vector ``center=[…]``
    argument — a self-test that the regex is not vacuous (issue #269
    follow-up: OpenSCAD's ``center`` is a single boolean; a vector is
    silently ignored, leaving the geometry uncentred — exactly the #269
    bug)."""
    assert _has_center_vector(
        "cube([40, 40, 10], center = [true, true, false]);"
    ) is True
    assert _has_center_vector(
        "cube([40, 40, 10], center=[true, true, false]);"
    ) is True
    assert _has_center_vector(
        "cylinder(h=16, center=[true, false, false]);"
    ) is True
    # center=true (a boolean, not a vector) is fine:
    assert _has_center_vector("cube([40, 40, 10], center=true);") is False
    assert _has_center_vector("cube([40, 40, 10], center = true);") is False
    # center on a non-primitive line is fine (e.g. a variable named center):
    assert (
        _has_center_vector("translate([0, -20, 0]) center = [0, 0];") is False
    )
    # No center at all:
    assert (
        _has_center_vector(
            "translate([-20, -20, 0]) cube([40, 40, 10]);"
        )
        is False
    )


def test_guard_detects_missing_feature() -> None:
    """The per-feature check must actually fail when a feature is missing
    from the ``_marker_scad()`` body (issue #269 self-test: the guard is
    not vacuous for the translate-based assertions either)."""
    # A body that is missing the post line:
    body_without_post = (
        'difference() {\n'
        '  translate([-20, -20, 0]) cube([40, 40, 10]);\n'
        '  translate([-8, -21, -1]) cube([16, 6, 12]);\n'
        '}\n'
        'translate([-32, -4, 0]) cube([8, 8, 22]);\n'
        'translate([-6, 22, 0]) cube([12, 12, 16]);\n'
        'translate([0, 0, 10]) cylinder(h = 16, r1 = 8, r2 = 0, $fn = 36);\n'
    )
    missing = [
        name
        for name, pattern in _MARKER_FEATURES
        if not pattern.search(body_without_post)
    ]
    assert "post" in missing, (
        f"expected 'post' to be missing from the synthetic body, but got "
        f"missing={missing!r} — the per-feature check is vacuous"
    )
    # The full body (all five features) should have nothing missing:
    full_body = (
        'difference() {\n'
        '  translate([-20, -20, 0]) cube([40, 40, 10]);\n'
        '  translate([-8, -21, -1]) cube([16, 6, 12]);\n'
        '}\n'
        'translate([24, -4, 0]) cube([8, 8, 45]);\n'
        'translate([-32, -4, 0]) cube([8, 8, 22]);\n'
        'translate([-6, 22, 0]) cube([12, 12, 16]);\n'
        'translate([0, 0, 10]) cylinder(h = 16, r1 = 8, r2 = 0, $fn = 36);\n'
    )
    missing_full = [
        name
        for name, pattern in _MARKER_FEATURES
        if not pattern.search(full_body)
    ]
    assert missing_full == [], (
        f"expected all five features to be present in the full synthetic "
        f"body, but got missing={missing_full!r}"
    )
