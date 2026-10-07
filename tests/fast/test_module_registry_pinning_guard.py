"""Fast-layer test: OpenSCAD image pinning (issue #392).

The render worker's ``FROM`` is digest-pinned (issue #348); this guard
extends the same discipline to every other OpenSCAD image reference in
production source:

- the module registry's ``DEFAULT_OPENSCAD_IMAGE`` must carry the
  ``@sha256:`` digest (never the rolling ``openscad/openscad:trixie``
  tag, whose 2026-09-28 roll broke headless PNG export);
- the digest constant in ``d33d/render_worker.py`` (single source of
  truth in Python) must stay equal to the ``Dockerfile`` FROM pin — the
  Dockerfile cannot import a Python constant, so "defined once" means
  "one Python constant + a matching Dockerfile line", and this test is
  what keeps the two from drifting apart on a future pin bump;
- no production code line may reference a rolling openscad image tag
  without a digest.

Docstring/comment lines that merely document the pin (e.g. naming the
rolling tag in order to say it is NOT used) are allowed — that is the
acceptance criterion's "docstrings/comments that merely document the
pin" carve-out. Whitelisted fixture/doc files (test argv strings, the
docs page, entrypoint.sh comments) are excluded by file.

No Docker required — this walks text files only.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import d33d.module_registry as mr
from d33d.render_worker import OPENSCAD_IMAGE_DIGEST

REPO_ROOT = Path(__file__).resolve().parents[2]

DIGEST = "sha256:0af06bc2aa7a45d18b01a23cfb9dae6dddcd9542611e7be50edea6beb3b52fa7"

#: An openscad image reference: ``openscad/openscad`` with an optional
#: ``:<tag>`` (any tag, including trixie), or a bare ``openscad:trixie``.
IMAGE_RE = re.compile(r"openscad/openscad(?::[^\s\"'`@,;)\]}]*)?|openscad:trixie")
DIGEST_ON_LINE_RE = re.compile(r"sha256:[0-9a-f]{64}")

#: A line ending in ``openscad/openscad@`` is a digest-pinned reference
#: whose ``sha256:…`` continues on the next line (implicit string
#: concatenation in ``render_worker.py``). Allow it.
DIGEST_AT_RE = re.compile(r'openscad/openscad@"?$')

#: Files where a rolling-tag mention is documentation or a deliberate
#: test fixture, not an image reference that would be ``docker run``-ed:
#: - docstrings in render_worker.py / module_registry.py that merely
#:   describe the pinned build or the tag deliberately NOT used;
#: - test fixtures that pass ``"openscad/openscad:trixie"`` as an
#:   arbitrary image string to pure argv-builders (no container is ever
#:   run in the fast suite);
#: - entrypoint.sh comments, the slow-suite docstrings, and the docs
#:   page that document the pin.
#: (Any NEW rolling-tag code reference in d33d/ fails this test by
#: construction — this whitelist covers only the pre-existing
#: docstring/fixture sites listed in the issue.)
WHITELISTED_FILES: frozenset[Path] = frozenset(
    {
        REPO_ROOT / "entrypoint.sh",
        REPO_ROOT / "tests" / "fast" / "test_run_container_timeout.py",
        REPO_ROOT / "tests" / "fast" / "test_views.py",
        REPO_ROOT / "tests" / "slow" / "test_module_registry_docker.py",
        REPO_ROOT / "tests" / "slow" / "test_render_views.py",
        REPO_ROOT / "tests" / "test_render_for_design_loop.py",
        REPO_ROOT / "docs" / "bosl2-pinning.md",
    }
)

#: Production source files walked by the guard (everything that could
#: ``docker run`` an image). Files outside ``d33d/`` are checked via the
#: file-level whitelist above.
PRODUCTION_SCAN_ROOTS: tuple[Path, ...] = (REPO_ROOT / "d33d",)


def _scan_files(root: Path, suffixes: set[str]) -> list[Path]:
    return sorted(
        p
        for p in root.rglob("*")
        if p.is_file() and p.suffix in suffixes and "__pycache__" not in p.parts
    )


def _docstring_lines(path: Path) -> set[int]:
    """Line numbers inside docstrings (module/class/function).

    Uses ``ast`` so the classification is precise: a docstring is an
    ``ast.Expr`` whose value is a string constant, attached to a
    module/class/function definition. Docstring lines are documentation
    (the "merely document the pin" carve-out), never code that flows
    into a ``docker run``.
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError:
        return set()
    lines: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            continue
        doc = ast.get_docstring(node, clean=False)
        if doc is None or not isinstance(node.body, list) or not node.body:
            continue
        first = node.body[0]
        if not (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            continue
        # The docstring spans from the first token of the string literal
        # (first.lineno) to its last line (first.end_lineno).
        lines.update(range(first.lineno, (first.end_lineno or first.lineno) + 1))
    return lines


def test_default_openscad_image_is_digest_pinned() -> None:
    """``DEFAULT_OPENSCAD_IMAGE`` must carry the ``@sha256:`` digest."""
    assert DIGEST in mr.DEFAULT_OPENSCAD_IMAGE
    assert mr.DEFAULT_OPENSCAD_IMAGE == OPENSCAD_IMAGE_DIGEST


def test_digest_constant_matches_dockerfile_from() -> None:
    """The Python digest constant and the Dockerfile FROM pin must be equal.

    The Dockerfile cannot import a Python constant, so "the digest is
    defined once" means one Python constant plus a matching Dockerfile
    line; this test is the guard that keeps a future pin bump from
    updating one side and not the other.
    """
    dockerfile = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")
    from_lines = [line for line in dockerfile.splitlines() if line.startswith("FROM")]
    assert len(from_lines) == 1
    assert f"openscad/openscad@{DIGEST}" in from_lines[0]
    assert OPENSCAD_IMAGE_DIGEST == f"docker.io/openscad/openscad@{DIGEST}"


def test_no_rolling_openscad_tag_in_production_code() -> None:
    """No production code line references a rolling openscad tag without a digest.

    A rolling reference is an ``openscad/openscad:<tag>`` or bare
    ``openscad:trixie`` appearing on a CODE line (not a comment, not a
    docstring) with no ``sha256:`` digest on that line. Docstring and
    comment lines that merely document the pin are allowed. Whitelisted
    fixture/doc files are excluded by file.
    """
    offenders: list[str] = []
    for root in PRODUCTION_SCAN_ROOTS:
        for path in _scan_files(root, {".py"}):
            if path in WHITELISTED_FILES:
                continue
            text = path.read_text(encoding="utf-8")
            doc_lines = _docstring_lines(path)
            for lineno, line in enumerate(text.splitlines(), 1):
                if not IMAGE_RE.search(line):
                    continue
                if DIGEST_ON_LINE_RE.search(line) or DIGEST_AT_RE.search(line):
                    continue
                if lineno in doc_lines or line.strip().startswith("#"):
                    continue
                offenders.append(
                    f"{path.relative_to(REPO_ROOT)}:{lineno}: {line.strip()}"
                )
    assert offenders == [], "\n".join(offenders)
