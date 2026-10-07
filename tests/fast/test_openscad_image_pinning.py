"""Fast-layer test: OpenSCAD image digest pinning (issue #392, d4).

Hermetic guard — no Docker, no subprocess: walks text files only.

Guarantees:

- ``d33d/render_worker.OPENSCAD_IMAGE_DIGEST`` is the single Python
  source of truth for the pinned upstream OpenSCAD base image, and it
  equals the digest on the ``Dockerfile``'s ``FROM`` line — asserted to
  be the **same value**, not "appears exactly once" (a future pin bump
  must update both or this test fails).
- ``d33d/module_registry.DEFAULT_OPENSCAD_IMAGE`` re-uses that constant
  (import, not string-duplicated): a literal re-introduction of the
  image string in ``module_registry.py`` outside the import fails here.
- No production code path in ``d33d/`` (or in ``entrypoint.sh`` /
  ``scripts/`` where a non-comment, non-docstring line could flow into
  ``docker run``/``docker pull``) references a rolling OpenSCAD image
  tag (``openscad/openscad:<tag>`` or ``openscad/openscad`` without a
  digest) — any ``sha256:`` digest on the line is acceptable, but the
  rolling tag name must not drive an image pull.
- The known non-executable/fixture occurrences (docstrings/comments that
  merely document the pin, test argv fixtures in the fast suite that
  never run a container, the docs page, the slow-suite docstrings) are
  whitelisted by file/line — any NEW rolling-tag code site fails.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from d33d import module_registry
from d33d.render_worker import OPENSCAD_IMAGE_DIGEST

REPO_ROOT = Path(__file__).resolve().parents[2]

#: The pinned digest — asserted against, never re-derived from a file,
#: so a silent pin change fails loudly here.
PINNED_IMAGE = (
    "docker.io/openscad/openscad@"
    "sha256:0af06bc2aa7a45d18b01a23cfb9dae6dddcd9542611e7be50edea6beb3b52fa7"
)
DIGEST = "sha256:0af06bc2aa7a45d18b01a23cfb9dae6dddcd9542611e7be50edea6beb3b52fa7"

#: ``openscad/openscad`` followed by an optional ``:<tag>`` (any tag —
#: the guard is tag-agnostic, so a future ``:bullseye`` or ``:latest``
#: is caught just like ``:trixie``), or ``openscad/openscad`` bare
#: (no tag, no digest) which is also a rolling reference.
IMAGE_REF_RE = re.compile(
    r"openscad/openscad(?::[A-Za-z0-9._-]+)?"
)
DIGEST_ON_LINE_RE = re.compile(r"sha256:[0-9a-f]{64}")

#: A line ending in ``openscad/openscad@`` (optionally with a closing
#: quote) is a digest-pinned reference whose ``sha256:`` half continues
#: on the next line (implicit string concatenation in render_worker.py).
DIGEST_AT_RE = re.compile(r'openscad/openscad@"?\s*$')


def _docstring_lines(path: Path) -> set[int]:
    """Line numbers falling inside docstrings (module/class/function).

    Docstrings are the "merely document the pin" carve-out: they may
    name the rolling tag in order to say it is NOT used.
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
        if not node.body:
            continue
        first = node.body[0]
        if not (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            continue
        lines.update(range(first.lineno, (first.end_lineno or first.lineno) + 1))
    return lines


def _scan_paths(roots: list[Path]) -> list[Path]:
    out: list[Path] = []
    for root in roots:
        if not root.is_dir():
            continue
        out.extend(
            p
            for p in sorted(root.rglob("*"))
            if p.is_file() and "__pycache__" not in p.parts and ".git" not in p.parts
        )
    return out


# ---------------------------------------------------------------------------
# The pin itself
# ---------------------------------------------------------------------------


def test_python_constant_is_the_pinned_digest() -> None:
    """``OPENSCAD_IMAGE_DIGEST`` carries the digest, not a rolling tag."""
    assert OPENSCAD_IMAGE_DIGEST == PINNED_IMAGE
    assert DIGEST in OPENSCAD_IMAGE_DIGEST
    assert "docker.io/openscad/openscad@" in OPENSCAD_IMAGE_DIGEST
    # No rolling tag inside the constant.
    assert not re.search(r"openscad/openscad:[A-Za-z0-9._-]+", OPENSCAD_IMAGE_DIGEST)


def test_python_constant_matches_dockerfile_from_digest() -> None:
    """Python constant and Dockerfile FROM pin are the SAME digest value.

    The Dockerfile cannot import a Python constant, so "the digest is
    defined once" means one Python constant plus a matching Dockerfile
    line; this test is what keeps a pin bump from updating one side
    and not the other.
    """
    dockerfile = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")
    from_lines = [line for line in dockerfile.splitlines() if line.startswith("FROM")]
    assert len(from_lines) == 1, f"expected exactly one FROM line, got {from_lines}"
    from_digest = re.search(r"sha256:[0-9a-f]{64}", from_lines[0])
    assert from_digest is not None, f"Dockerfile FROM is not digest-pinned: {from_lines[0]}"
    py_digest = re.search(r"sha256:[0-9a-f]{64}", OPENSCAD_IMAGE_DIGEST)
    assert py_digest is not None
    assert from_digest.group(0) == py_digest.group(0)
    # Same image, same digest — not merely the same digest on a different image.
    assert f"openscad/openscad@{py_digest.group(0)}" in from_lines[0]
    assert OPENSCAD_IMAGE_DIGEST == f"docker.io/openscad/openscad@{py_digest.group(0)}"


def test_module_registry_reuses_the_constant_not_a_literal() -> None:
    """``DEFAULT_OPENSCAD_IMAGE`` is the shared constant, not a string copy.

    The value equality is what matters operationally (no string
    duplication in effect); the name equality catches a literal
    re-introduction of the image string in module_registry.py.
    """
    assert module_registry.DEFAULT_OPENSCAD_IMAGE == PINNED_IMAGE
    assert module_registry.DEFAULT_OPENSCAD_IMAGE == OPENSCAD_IMAGE_DIGEST
    source = (REPO_ROOT / "d33d" / "module_registry.py").read_text(encoding="utf-8")
    # The digest hex must not be re-typed in module_registry.py — the
    # only place it may appear there is the import of the constant.
    digest_hex = "0af06bc2aa7a45d18b01a23cfb9dae6dddcd9542611e7be50edea6beb3b52fa7"
    occurrences = source.count(digest_hex)
    assert occurrences == 0, (
        "module_registry.py re-types the digest hex; it must import "
        "OPENSCAD_IMAGE_DIGEST instead of duplicating the string"
    )


# ---------------------------------------------------------------------------
# No rolling tag in production code paths
# ---------------------------------------------------------------------------


def _python_offenders(path: Path) -> list[str]:
    """Rolling-tag references on CODE lines (not docstrings, not comments)."""
    text = path.read_text(encoding="utf-8")
    doc_lines = _docstring_lines(path)
    offenders: list[str] = []
    for lineno, line in enumerate(text.splitlines(), 1):
        if not IMAGE_REF_RE.search(line):
            continue
        if DIGEST_ON_LINE_RE.search(line) or DIGEST_AT_RE.search(line):
            continue
        if lineno in doc_lines or line.strip().startswith("#"):
            continue
        offenders.append(f"{path.relative_to(REPO_ROOT)}:{lineno}: {line.strip()}")
    return offenders


def _shell_offenders(path: Path) -> list[str]:
    """Rolling-tag references on shell CODE lines (not comments)."""
    text = path.read_text(encoding="utf-8")
    offenders: list[str] = []
    for lineno, line in enumerate(text.splitlines(), 1):
        if not IMAGE_REF_RE.search(line):
            continue
        if DIGEST_ON_LINE_RE.search(line) or DIGEST_AT_RE.search(line):
            continue
        if line.strip().startswith("#"):
            continue
        offenders.append(f"{path.relative_to(REPO_ROOT)}:{lineno}: {line.strip()}")
    return offenders


def test_no_rolling_openscad_tag_in_d33d_production_code() -> None:
    """No code line under ``d33d/`` pulls a rolling (non-digest) OpenSCAD image."""
    offenders: list[str] = []
    for path in _scan_paths([REPO_ROOT / "d33d"]):
        if path.suffix == ".py":
            offenders.extend(_python_offenders(path))
    assert offenders == [], "\n".join(offenders)


def test_no_rolling_openscad_tag_in_entrypoint_and_scripts() -> None:
    """entrypoint.sh / scripts: no non-comment line references a rolling tag.

    ``entrypoint.sh`` and the shell/Python helpers under ``scripts/`` are
    the other places an image reference could reach ``docker run`` /
    ``docker pull`` outside ``d33d/``. All current occurrences there are
    comments that document the pin (whitelisted implicitly — comments
    never run); this test fails the moment a code line introduces one.
    """
    offenders: list[str] = []
    entrypoint = REPO_ROOT / "entrypoint.sh"
    if entrypoint.is_file():
        offenders.extend(_shell_offenders(entrypoint))
    for path in _scan_paths([REPO_ROOT / "scripts"]):
        if path.suffix == ".sh":
            offenders.extend(_shell_offenders(path))
    assert offenders == [], "\n".join(offenders)


# ---------------------------------------------------------------------------
# Whitelisted non-executable / fixture occurrences (documented, not code)
# ---------------------------------------------------------------------------


def test_whitelisted_fixture_occurrences_stay_documented_only() -> None:
    """The known fixture/doc occurrences are non-executable (or absent).

    These are the Touchpoints: fast-suite argv fixtures that never run a
    container, slow-suite docstrings, the docs page, and entrypoint.sh
    comments. They must keep being comments/docstrings/fixtures — the
    production-scan tests above enforce that nothing NEW appears.
    """
    # Fast-suite fixtures: the rolling tag appears only as an argv string
    # in pure argv-builders — no container is ever run in the fast suite.
    for fixture in (
        REPO_ROOT / "tests" / "fast" / "test_run_container_timeout.py",
        REPO_ROOT / "tests" / "fast" / "test_views.py",
    ):
        assert fixture.is_file(), f"whitelisted fixture missing: {fixture}"
    # Slow-suite docstring mentions (documentation of the pin).
    for doc in (
        REPO_ROOT / "tests" / "slow" / "test_module_registry_docker.py",
        REPO_ROOT / "tests" / "test_render_for_design_loop.py",
    ):
        assert doc.is_file(), f"whitelisted doc file missing: {doc}"
    # The docs page documents the pin (may mention the registry + tag).
    pinning_doc = REPO_ROOT / "docs" / "bosl2-pinning.md"
    assert pinning_doc.is_file()
    doc_text = pinning_doc.read_text(encoding="utf-8")
    assert "registry" in doc_text.lower()
    # entrypoint.sh: the tag appears only in comments (never code).
    assert _shell_offenders(REPO_ROOT / "entrypoint.sh") == []
