"""Fast-layer test: entrypoint.sh's awk bbox parser is translation-invariant.

Regression guard against the #223 class of bug: if the awk ever grows a
min-vs-origin or max-vs-origin dependency, two identical-shape models
translated to different positions in space would yield different max_extent
values, silently re-introducing position-dependent camera fitting.

The awk program is extracted verbatim from the static ``entrypoint.sh`` text
(no bash execution, no Docker) and run on ASCII STL fixtures of known
shape. Both the absolute position and the exact extents are checked.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import d33d.render_worker as rw  # noqa: F401  (import guard: file must exist)

ENTRYPOINT = Path(__file__).resolve().parents[2] / "entrypoint.sh"
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "stl"


def _extract_awk_bbox_program(src: str) -> str:
    """Extract the awk bbox program body from ``entrypoint.sh``.

    The program sits inside ``bbox_out=$(awk ' ... ' "${STL_FILE}")``.
    The body (between the opening and closing single-quotes) contains no
    single-quote characters of its own (the only ``'`` in the body would
    break the bash quoting — the entrypoint authors must keep it that
    way). Capture from the first ``'`` after ``bbox_out=$(awk`` to the
    next ``'`` (which closes the awk program, immediately followed by the
    double-quoted ``"${STL_FILE}"`` argument).
    """
    m = re.search(
        r"bbox_out=\$\(awk '([^']*)'\s*\"\$\{STL_FILE\}\"", src, re.DOTALL
    )
    assert m is not None, "awk bbox program not found in entrypoint.sh"
    return m.group(1)


def _run_awk(program: str, stl_path: Path) -> float:
    """Run the awk program against an ASCII STL and return the parsed
    ``max_extent`` (a float). The program is executed via ``awk`` (mawk or
    gawk both accepted; the CI base image ships mawk). The program emits
    ``"<max_extent> <cx> <cy> <cz>"`` (the bbox centre fields are the
    issue #223 camera-translate substitution); this test only needs the
    max_extent, so the first field is taken and the rest discarded."""
    proc = subprocess.run(
        ["awk", program, str(stl_path)],
        capture_output=True,
        check=False,
    )
    assert proc.returncode == 0, (
        f"awk bbox program failed on {stl_path.name}: "
        f"{proc.stderr.decode()}"
    )
    out = proc.stdout.decode().strip()
    max_extent_field = out.split()[0]
    return float(max_extent_field)


def test_awk_bbox_knows_each_golden_fixture_extent() -> None:
    """The awk bbox program must compute the correct ``max_extent`` for
    each of the two golden asymmetric fixtures (issue #223).

    golden-a: x[-22.5, 45.01], y[0, 20.1], z[0, 30.01]  → 67.51
    golden-b: x[0, 40.1], y[-18.15, 20], z[0, 30.01]    → 40.10

    These are distinct shapes (different max extents); the parser must
    emit the correct value for each, and the two values must differ
    (i.e. the parser is actually reading the geometry, not emitting a
    constant).
    """
    src = ENTRYPOINT.read_text(encoding="utf-8")
    program = _extract_awk_bbox_program(src)

    a = _run_awk(program, FIXTURES / "issue-223-asymmetric-a.stl")
    b = _run_awk(program, FIXTURES / "issue-223-asymmetric-b.stl")

    assert a != b, (
        f"awk bbox returned identical max_extent for different shapes: "
        f"{a} vs {b}"
    )
    assert abs(a - 67.51) < 1e-3, f"golden-a max_extent {a} != expected 67.51"
    assert abs(b - 40.10) < 1e-3, f"golden-b max_extent {b} != expected 40.10"


def test_awk_bbox_translation_invariance() -> None:
    """Explicit translation-invariance: the awk bbox program must yield
    an identical ``max_extent`` for two identical-shape STLs translated
    to different absolute positions in space.

    A position-dependent parser (min-vs-origin or max-vs-origin
    instead of max-minus-min) would silently re-introduce the #223
    class of bug: the camera fit would depend on where the model sits
    in the coordinate system, not on its shape.

    The test generates a translated copy of the golden-a fixture in
    a temp file (every vertex shifted +100 in x and +100 in y) and
    asserts the awk emits an identical ``max_extent`` for the original
    and the translated copy.
    """
    src = ENTRYPOINT.read_text(encoding="utf-8")
    program = _extract_awk_bbox_program(src)

    src_path = FIXTURES / "issue-223-asymmetric-a.stl"
    assert src_path.is_file(), "golden-a fixture missing"
    original = _run_awk(program, src_path)

    # Build a translated copy in /tmp by shifting every vertex by +100
    # in x and +100 in y. The shape is unchanged; only the absolute
    # position differs.
    import tempfile

    with tempfile.NamedTemporaryFile(
        "w", suffix=".stl", delete=False, prefix="bbox-test-"
    ) as f:
        for line in src_path.read_text(encoding="utf-8").splitlines(keepends=True):
            if line.lstrip().startswith("vertex "):
                parts = line.split()
                assert len(parts) >= 4 and parts[0] == "vertex"
                x = float(parts[1]) + 100.0
                y = float(parts[2]) + 100.0
                z = float(parts[3])
                indent = line[: len(line) - len(line.lstrip())]
                f.write(f"{indent}vertex {x} {y} {z}\n")
            else:
                f.write(line)
        translated_path = f.name

    try:
        translated = _run_awk(program, Path(translated_path))
    finally:
        Path(translated_path).unlink(missing_ok=True)

    assert translated == original, (
        f"awk bbox is position-dependent: original={original}, "
        f"translated(+100,+100)={translated}"
    )
