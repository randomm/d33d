"""Fast guard: no ``v=`` argument in ``cube()``/``cylinder()``/``sphere()``
calls inside the ``_marker_scad`` model string (issue #269) or any
``tests/fixtures/scad/*.scad`` file.

OpenSCAD has no ``v`` parameter on ``cube``/``cylinder``/``sphere``. It is
silently ignored (with a warning), so a misplaced ``v=`` silently produces
geometry at the origin instead of at the intended offset. The slow-test
marker model (``_marker_scad``) and the #234 framing fixtures both shipped
with this bug (issue #269), which made every asymmetric feature collapse to
the origin corner and defeated the per-view placement assertions.

This guard scans:
  1. The ``_marker_scad()`` model string — imported from the slow test
     module (``tests/slow/test_render_views.py``) so the guard checks the
     string the slow test actually renders, not a text-parsed copy of the
     function body.
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
``translate`` rather than ``v=``). ``test_marker_scad_is_translate_based``
asserts that per-line on the imported string, so a re-introduced ``v=`` on
any of the five lines fails the guard.

The scanner is multi-line aware: each primitive call is joined across line
breaks by tracking paren depth from ``cube(``/``cylinder(``/``sphere(`` to
its matching ``)``, and only the call text is scanned. This catches a
``v=``/``center=[`` that sits on a line by itself (e.g. a ``cube(`` whose
args continue on the next line) and — because ``rotate(v=…)`` is not one of
the three primitives — it no longer false-positives on a ``rotate(v=…)``
immediately preceding a ``cube(…)`` on the same physical line.
"""

from __future__ import annotations

import re
from pathlib import Path

from tests.slow.test_render_views import _marker_scad

FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "scad"

# The three primitives that have no ``v``/vector-``center`` parameter.
_PRIM_RE = re.compile(r"\b(cube|cylinder|sphere)\s*\(")

# A ``v=`` or ``v =`` argument (with optional spaces around ``=``, and
# ensuring the preceding char is not part of a longer identifier, e.g.
# ``xv=`` or ``$v=`` — the ``v`` is not at the start of a real identifier).
_V_ARG_RE = re.compile(r"(?<![\w$])v\s*=")

# A vector ``center=[…]`` argument (issue #269 follow-up: OpenSCAD's
# ``center`` is a single boolean; a vector is silently ignored, leaving
# the geometry uncentred — the exact bug the #269 fix was for).
_CENTER_VECTOR_RE = re.compile(r"(?<![\w$])center\s*=\s*\[")

# The five feature lines of ``_marker_scad()`` that issue #269 requires to
# be ``translate()``-based. Each entry is ``(feature name, a regex that must
# be found inside the ``_marker_scad`` model string)``.
#
#   - post:          the tall +X post, ``translate([24, -4, 0])`` + ``cube([8, 8, 45])``
#   - short block:   the −X short block, ``translate([-32, -4, 0])`` + ``cube([8, 8, 22])``
#   - +Y block:      the +Y block, ``translate([-6, 22, 0])`` + ``cube([12, 12, 16])``
#   - cone:          the top-centre cone,
#                    ``translate([0, 0, 10])`` + ``cylinder(h=16, r1=8, r2=0, $fn=36)``
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


def _strip_comments_and_strings(text: str) -> str:
    """Strip line comments (SCAD's ``//`` and Python's ``#``) and
    (single- or triple-)quoted string bodies from SCAD/Python source,
    preserving line structure.

    The guard only cares about real geometry calls; a ``v=`` or
    ``center=[…`` that appears inside a comment or a docstring must not be
    flagged (and must not break the paren-depth call join). Replacing the
    characters (rather than deleting them) keeps line numbers and offsets
    aligned with the original text so reported line numbers stay correct.
    """
    out: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        c = text[i]
        # Line comment (``#`` in Python, ``//`` in SCAD): everything from
        # the marker to end of line is stripped. A comment marker inside a
        # string is handled by the string branch first (which runs before
        # this one for the opening quote).
        if c == "#" or (c == "/" and text[i + 1 : i + 2] == "/"):
            j = text.find("\n", i)
            if j == -1:
                j = n
            out.append(" " * (j - i))
            i = j
        # Triple-quoted string (docstrings, embedded SCAD strings).
        elif c in "\"'" and text[i : i + 3] == c * 3:
            quote = c * 3
            j = text.find(quote, i + 3)
            if j == -1:
                j = n - 3
            else:
                j += 2  # keep the closing delimiter's position sane
            out.append(" " * (j - i))
            i = j + 3
        # Single-line string (single or double quoted).
        elif c in "\"'":
            quote = c
            j = i + 1
            while j < n and text[j] != quote and text[j] != "\n":
                j += 1
            out.append(" " * (j - i))
            i = j
        else:
            out.append(c)
            i += 1
    return "".join(out)


def _find_primitive_calls(text: str) -> list[tuple[int, str]]:
    """Return ``(start_line, call_text)`` for each ``cube``/``cylinder``/
    ``sphere`` call in ``text``, joining the call across line breaks by
    tracking paren depth from the primitive's opening ``(`` to its matching
    ``)``.

    The call text includes the matched primitive name and its full argument
    list (up to and including the closing paren). Comments and strings are
    stripped first so a ``#`` or quote inside the call cannot confuse the
    depth counter. ``start_line`` is the 1-indexed line of the primitive
    name in the (comment/string-stripped) text — which is line-aligned with
    the original.
    """
    calls: list[tuple[int, str]] = []
    for m in _PRIM_RE.finditer(text):
        open_paren = text.index("(", m.start())
        depth = 0
        j = open_paren
        n = len(text)
        while j < n:
            ch = text[j]
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        if depth != 0:
            # Unbalanced (should not happen in valid SCAD); take to EOL.
            j = text.find("\n", open_paren)
            if j == -1:
                j = n
        start_line = text.count("\n", 0, m.start()) + 1
        calls.append((start_line, text[m.start() : j + 1]))
    return calls


def _scan_source(text: str) -> list[tuple[int, str]]:
    """Return ``(line_no, snippet)`` pairs for every primitive call in
    ``text`` that contains a ``v=`` argument OR a vector ``center=[…]``
    argument.

    Multi-line aware: only the joined call text is scanned, so an argument
    on a continuation line is caught, and a ``rotate(v=…)`` immediately
    before a ``cube(…)`` on the same physical line is NOT flagged (it is not
    part of the ``cube`` call).
    """
    stripped = _strip_comments_and_strings(text)
    hits: list[tuple[int, str]] = []
    for start_line, call_text in _find_primitive_calls(stripped):
        if _V_ARG_RE.search(call_text) or _CENTER_VECTOR_RE.search(call_text):
            hits.append((start_line, call_text.replace("\n", " ").strip()))
    return hits


def _marker_scad_body() -> str:
    """The SCAD model string the slow test renders — imported so the guard
    checks the exact string, not a text-parsed copy."""
    return _marker_scad()


def test_no_v_arg_in_marker_scad() -> None:
    """The ``_marker_scad()`` model string must contain no ``v=`` argument
    AND no vector ``center=[…]`` argument on any ``cube``/``cylinder``/
    ``sphere`` call (issue #269 + follow-up).

    The #234 framing fixtures and the ``_marker_scad`` body both previously
    used ``v=`` — the bug that made the marker model degenerate. The
    follow-up to #269 found the slab still used ``center=[true, true,
    false]`` — a vector OpenSCAD silently ignores (``center`` is a single
    boolean) — so the slab was still uncentred. The operator decision
    (issue #269 + follow-up) is that NO line may keep ``v=`` OR a vector
    ``center=[…]``: the slab must really be centred via ``translate``, and
    every feature and the notch cutter must use ``translate``.
    """
    body = _marker_scad_body()
    hits = _scan_source(body)
    assert hits == [], (
        "v= or center=[…] argument found on a cube/cylinder/sphere call in "
        "the _marker_scad() model string (issue #269: OpenSCAD has no v= "
        "parameter and center is a boolean, not a vector — use translate "
        "instead):\n"
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
        for lineno, snippet in _scan_source(text):
            all_hits.append((scad.name, lineno, snippet))
    assert all_hits == [], (
        "v= or center=[…] argument found on a cube/cylinder/sphere call in "
        "SCAD fixtures (issue #269: OpenSCAD has no v= parameter and center "
        "is a boolean, not a vector): \n"
        + "\n".join(f"  {name}:{n}: {line}" for name, n, line in all_hits)
    )


def test_marker_scad_is_translate_based() -> None:
    """The ``_marker_scad()`` model string must be a ``translate()``-based
    model (issue #269): each of the five features (post, short block,
    +Y block, cone, difference-block) must be present, each feature's
    geometry placed with ``translate`` (or, for the difference-block, the
    ``difference()`` construct).

    This per-feature check is the guard that the file-wide ``v=`` scan
    cannot express on its own: it asserts that the specific SCAD string
    contains the expected ``translate()`` calls (or, for the difference
    block, the ``difference()`` construct). The ``v=``/``center=[`` scan
    (``test_no_v_arg_in_marker_scad``) is the complementary check that no
    forbidden argument survives.
    """
    body = _marker_scad_body()
    missing = [
        name for name, pattern in _MARKER_FEATURES if not pattern.search(body)
    ]
    assert missing == [], (
        "expected feature(s) missing from the _marker_scad() model string "
        f"(issue #269: the five feature lines must each be present in the "
        f"translate-based marker model): missing {missing!r}"
    )


def test_guard_detects_synthetic_v_arg() -> None:
    """The guard's scanner must actually detect a ``v=`` argument — a
    self-test that the scan is not vacuous (issue #269)."""
    # v= on the cube call (same line):
    assert (
        _scan_source("cube([8, 8, 45], v=[24, -4, 0]);")
    ) == [(1, "cube([8, 8, 45], v=[24, -4, 0])")]
    # v= on the cylinder call (same line):
    assert (
        _scan_source("cylinder(h=16, r1=8, r2=0, v=[0,0,10]);")
    ) == [(1, "cylinder(h=16, r1=8, r2=0, v=[0,0,10])")]
    # v= on the sphere call (same line):
    assert (
        _scan_source("sphere([8], v=[0,0,10]);")
    ) == [(1, "sphere([8], v=[0,0,10])")]
    # v= on a CONTINUATION line (multi-line aware — this is the new
    # capability the per-line scan could not express). The returned snippet
    # is the call text from the comment/string-stripped source, so newlines
    # are preserved but the spaces in the stripped input are what the scan
    # saw — assert the hit is found (line 1) and contains the v= arg.
    multi = _scan_source("cube(\n [1,1,1],\n v=[0,0,0])")
    assert len(multi) == 1 and multi[0][0] == 1 and "v=[0,0,0]" in multi[0][1], (
        f"multi-line v= arg on a cube( call was not caught: {multi!r}"
    )
    # $v= is not a real param; exclude it:
    assert (
        _scan_source("cube([8, 8, 45], $v=[24, -4, 0]);")
    ) == []
    # rotate(v=...) immediately before a cube: NOT a cube/cylinder/sphere
    # call, so it must NOT be flagged (the per-line scan false-positived
    # here; the call-join does not):
    assert (
        _scan_source("rotate(v=[0,0,1]) cube(5);")
    ) == []
    # v= on a different (non-primitive) call:
    assert (
        _scan_source("translate(v=[1,2,3]);")
    ) == []


def test_guard_detects_synthetic_center_vector() -> None:
    """The guard's scanner must actually detect a vector ``center=[…]``
    argument — a self-test that the scan is not vacuous (issue #269
    follow-up: OpenSCAD's ``center`` is a single boolean; a vector is
    silently ignored, leaving the geometry uncentred — exactly the #269
    bug)."""
    assert (
        _scan_source("cube([40, 40, 10], center = [true, true, false]);")
    ) == [(1, "cube([40, 40, 10], center = [true, true, false])")]
    assert (
        _scan_source("cube([40, 40, 10], center=[true, true, false]);")
    ) == [(1, "cube([40, 40, 10], center=[true, true, false])")]
    assert (
        _scan_source("cylinder(h=16, center=[true, false, false]);")
    ) == [(1, "cylinder(h=16, center=[true, false, false])")]
    # center=true (a boolean, not a vector) is fine:
    assert (
        _scan_source("cube([40, 40, 10], center=true);")
    ) == []
    assert (
        _scan_source("cube([40, 40, 10], center = true);")
    ) == []
    # center on a non-primitive call is fine (a variable named center):
    assert (
        _scan_source("translate([0, -20, 0]) center = [0, 0];")
    ) == []
    # No center at all:
    assert (
        _scan_source("translate([-20, -20, 0]) cube([40, 40, 10]);")
    ) == []


def test_guard_strips_comments_and_strings() -> None:
    """A ``v=``/``center=[`` inside a comment or string must NOT be
    flagged — the scanner strips comments and string bodies (issue #269).
    """
    # A # comment mentioning v= on the same physical line as a clean cube:
    assert (
        _scan_source("cube([1,1,1]); // not a comment; now a real comment: v=...")
    ) == []
    # A # line comment on its own line:
    assert (
        _scan_source("cube([1,1,1]);\n# v=[1,2,3] is forbidden here\n")
    ) == []
    # A // line comment (SCAD-style) on the same line as a clean cube:
    #   OpenSCAD uses // for line comments, so both // and # must be
    #   stripped (a '#' inside a // comment is not a string boundary).
    assert (
        _scan_source("cube([1,1,1]); // v=[1,2,3] is a bad arg\n")
    ) == []
    # A string literal containing a cube( with v= must not be flagged:
    assert (
        _scan_source('echo("cube([1,1,1], v=[0,0,0]);");')
    ) == []
    # A triple-quoted Python docstring containing v= must not be flagged:
    assert (
        _scan_source('"""\n cube(v=[0,0,0])\n """')
    ) == []


def test_guard_detects_missing_feature() -> None:
    """The per-feature check must actually fail when a feature is missing
    from the ``_marker_scad()`` model string (issue #269 self-test: the
    guard is not vacuous for the translate-based assertions either)."""
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
