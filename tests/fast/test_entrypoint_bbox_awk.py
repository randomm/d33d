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
import tempfile
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


def _validation_block_source(src: str) -> str:
    """Extract the inner else-branch body of the bbox validation block.

    The validation logic (read -r, per-field numeric check, max_extent=0
    check) lives inside the else branch of the outer if/else that tests
    bbox_rc and bbox_out. This helper extracts just that else-branch body
    so it can be executed in a sandbox bash with a stubbed bbox_out.
    """
    read_idx = src.index("read -r max_extent BBOX_CX BBOX_CY BBOX_CZ")
    else_idx = src.rindex("else", 0, read_idx)
    outer_fi = src.index("fi\nfi\n", read_idx)
    return src[else_idx + 5 : outer_fi]


def test_bbox_validation_aborts_on_malformed_field() -> None:
    """The shell-side validation block must abort (bbox_status=1) when any
    of the four awk output fields (max_extent, cx, cy, cz) is non-numeric
    or empty — not just when max_extent is 0.

    The entrypoint's awk always emits well-formed %.10f fields, so this
    guard pins the *validation code itself* against regressions: if a
    future edit removes the per-field numeric check (leaving only the
    legacy max_extent=0 test), a malformed centre field would silently
    produce an unframeable camera string.

    The test extracts the actual validation block from ``entrypoint.sh``
    and runs it in a sandbox bash with a stubbed ``bbox_out`` carrying a
    malformed field (e.g. "abc" for BBOX_CY). A correct block sets
    bbox_status=1; a regressed block (max_extent-only check) would not.
    """
    src = ENTRYPOINT.read_text(encoding="utf-8")
    block = _validation_block_source(src)

    # The block must reference all four field variables — a regression to
    # max_extent-only validation would drop BBOX_CX/CY/CZ from the check.
    for var in ("max_extent", "BBOX_CX", "BBOX_CY", "BBOX_CZ"):
        assert var in block, (
            f"bbox validation block no longer references {var} "
            f"(issue #227: all four fields must be validated)"
        )

    # Run the block with a malformed centre field. The block sits in a
    # context where bbox_out is already set; here we pre-set bbox_out to
    # a value with BBOX_CY="abc" and verify bbox_status becomes 1.
    script = (
        "set -euo pipefail\n"
        "bbox_status=0\n"
        "bbox_detail=\"\"\n"
        "bbox_rc=0\n"
        "bbox_out=\"30.0000000000 5.0000000000 abc 15.0000000000\"\n"
        + block
        + "printf \"%s\\n\" \"$bbox_status\" >&2\n"
    )
    proc = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        check=False,
    )
    assert proc.returncode == 0, (
        f"validation block failed to run: {proc.stderr.decode()}"
    )
    status = proc.stderr.decode().strip().splitlines()[-1]
    assert status == "1", (
        f"bbox validation did not abort on malformed BBOX_CY (got "
        f"bbox_status={status!r}, expected 1)"
    )


def _run_awk_guard_case(stl_text: str, src: str) -> tuple[int, str]:
    """Run the REAL awk bbox program from ``entrypoint.sh`` against an
    in-memory ASCII STL, then run the REAL validation block against
    awk's actual stdout.

    Returns ``(bbox_status, bbox_detail)`` — the harness reproduces the
    entrypoint's shell context (``set -euo pipefail``, ``bbox_status=0``,
    ``bbox_rc`` unset so ``${bbox_rc:-0}`` defaults like in production,
    and the ASCII first-line check before the awk invocation) so the
    if/elif/else ordering behaves exactly as in the entrypoint.
    """
    import tempfile

    program = _extract_awk_bbox_program(src)
    block = _validation_block_source(src)
    with tempfile.NamedTemporaryFile(
        "w", suffix=".stl", delete=False, prefix="bbox-guard-"
    ) as f:
        f.write(stl_text)
        stl_path = f.name
    try:
        script = (
            "set -euo pipefail\n"
            'stl_first_line=$(awk "NR == 1 { print; exit }" "$1")\n'
            "bbox_status=0\n"
            'bbox_detail=""\n'
            "if [ \"${stl_first_line}\" != \"\" ] && ! printf '%s' \"${stl_first_line}\" | grep -q '^solid[[:space:]]'; then\n"
            "    bbox_status=1\n"
            '    bbox_detail="STL is not ASCII (first line is not solid)"\n'
            "else\n"
            "    bbox_out=$(awk '" + program + "' \"$1\") || bbox_rc=$?\n"
            '    if [ "${bbox_rc:-0}" -ne 0 ]; then\n'
            "        bbox_status=1\n"
            '        bbox_detail="awk bbox parse exited non-zero (exit ${bbox_rc})"\n'
            '    elif [ -z "${bbox_out}" ]; then\n'
            "        bbox_status=1\n"
            '        bbox_detail="awk bbox parse produced no output (truncated STL)"\n'
            "    else\n"
            + block
            + "    fi\n"
            "fi\n"
            'printf "%s|%s\\n" "$bbox_status" "$bbox_detail"\n'
        )
        proc = subprocess.run(
            ["bash", "-c", script, "", stl_path],
            capture_output=True,
            check=False,
        )
    finally:
        Path(stl_path).unlink(missing_ok=True)
    assert proc.returncode == 0, (
        f"harness failed for STL:\n{stl_text}\nstderr: {proc.stderr.decode()}"
    )
    status_line = proc.stdout.decode().strip().splitlines()[-1]
    status, _, detail = status_line.partition("|")
    return int(status), detail


def test_bbox_awk_non_numeric_vertices_trip_shell_abort() -> None:
    """D4(a): vertex lines with non-numeric junk must abort the render.

    awk coerces any non-numeric field to 0 (``"abc" + 0 == 0``), so an
    ASCII STL whose ``vertex`` lines carry junk text silently produces
    an all-zero bbox — indistinguishable from an empty model unless the
    shell guards it. This test feeds the REAL awk program from
    ``entrypoint.sh`` junk-carrying vertex lines, confirms awk itself
    emits the all-zero line (the coercion premise), then confirms the
    REAL validation block aborts (bbox_status=1) via the
    no-vertex-lines path — the same abort the empty-model case uses.
    """
    src = ENTRYPOINT.read_text(encoding="utf-8")
    # All vertex coordinates are junk text: awk coerces each to 0
    # ("abc" + 0 == 0), so the parsed bbox is exactly the zero line.
    stl = (
        "solid guard\n"
        "  facet normal 0 0 0\n"
        "    vertex abc def ghi\n"
        "    vertex jkl mno pqr\n"
        "  endfacet\n"
        "endsolid guard\n"
    )

    # Premise: the awk program coerces the junk to 0 and emits the
    # all-zero line (max_extent 0 plus zero centre).
    program = _extract_awk_bbox_program(src)
    with tempfile.NamedTemporaryFile(
        "w", suffix=".stl", delete=False, prefix="bbox-coerce-"
    ) as f:
        f.write(stl)
        stl_path = f.name
    try:
        raw = subprocess.run(
            ["awk", program, stl_path], capture_output=True, check=False
        )
    finally:
        Path(stl_path).unlink(missing_ok=True)
    assert raw.returncode == 0
    fields = raw.stdout.decode().split()
    assert len(fields) == 4 and all(
        float(v) == 0.0 for v in fields
    ), f"expected all-zero bbox from junk vertices, got {raw.stdout.decode()!r}"

    # The shell-side guard must turn that all-zero output into an abort.
    status, detail = _run_awk_guard_case(stl, src)
    assert status == 1, (
        f"junk-vertex STL did not abort (bbox_status={status}, "
        f"detail={detail!r})"
    )
    assert "no vertex lines parsed" in detail, (
        f"unexpected abort detail for junk-vertex STL: {detail!r}"
    )


def test_bbox_validation_checks_all_four_fields_and_awk_rc() -> None:
    """D4(b): source-level assertion that the validation block checks all
    four awk output fields AND the awk subprocess exit status.

    The per-field numeric check and the rc capture are the #227 guards;
    a regression that drops BBOX_CX/CY/CZ from the validation loop or
    deletes the ``|| bbox_rc=$?`` capture would silently let a malformed
    centre (or a failed awk invocation) through to the camera string.
    This guard pins both directly in the entrypoint source.
    """
    src = ENTRYPOINT.read_text(encoding="utf-8")

    # (i) awk rc must be captured at the subprocess boundary.
    assert '|| bbox_rc=$?' in src, (
        "entrypoint.sh no longer captures the awk bbox subprocess exit "
        "status (issue #227: '|| bbox_rc=$?' not found)"
    )
    # (ii) The rc must actually be checked against 0 (a capture with no
    #     check is dead code).
    assert '"${bbox_rc:-0}" -ne 0' in src or '"${bbox_rc}" -ne 0' in src, (
        "entrypoint.sh captures bbox_rc but never checks it against 0 "
        "(issue #227)"
    )

    # (iii) The validation block itself must reference all four field
    #     variables — a regression to max_extent-only validation would drop
    #     BBOX_CX/CY/CZ from the check (the malformed-field abort tests
    #     above pin the behaviour; this pins the source directly).
    block = _validation_block_source(src)
    for var in ("max_extent", "BBOX_CX", "BBOX_CY", "BBOX_CZ"):
        assert var in block, (
            f"bbox validation block no longer references {var} "
            f"(issue #227: all four fields must be validated)"
        )


def test_bbox_validation_passes_negative_centre() -> None:
    """The shell-side numeric validation must accept negative centre
    coordinates (a legitimate bbox centre can be negative — issue #223's
    fixtures sit in negative coordinate space).

    A naive digits-only pattern (no leading-minus) would reject valid
    negative centres and abort real renders. This test runs the actual
    validation block with all four fields well-formed but two of them
    negative, and asserts bbox_status stays 0 (no abort).
    """
    src = ENTRYPOINT.read_text(encoding="utf-8")
    block = _validation_block_source(src)

    script = (
        "set -euo pipefail\n"
        "bbox_status=0\n"
        "bbox_detail=\"\"\n"
        "bbox_rc=0\n"
        "bbox_out=\"30.0000000000 -12.5000000000 -5.0000000000 2.5000000000\"\n"
        + block
        + "printf \"%s\\n\" \"$bbox_status\" >&2\n"
    )
    proc = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        check=False,
    )
    assert proc.returncode == 0, (
        f"validation block failed to run: {proc.stderr.decode()}"
    )
    status = proc.stderr.decode().strip().splitlines()[-1]
    assert status == "0", (
        f"bbox validation aborted on a valid negative-centre bbox "
        f"(bbox_status={status!r}, expected 0 — negative centres are "
        f"legitimate, see issue #223)"
    )
