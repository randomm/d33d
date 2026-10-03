"""Part-fixture path containment for the eval harness (issue #340).

The imported-part cases (and the run.py staging path) reference a mesh
fixture under ``evals/cases/fixtures/``. The path is user-supplied case
data, so it must be contained in that directory: a ``../`` traversal, an
absolute path, or a symlink escape is a per-case violation, never a
crash. This module is the single home for that rule.
"""

from __future__ import annotations

from pathlib import Path


def check_fixture_containment(repo_root: Path, fixture: str) -> tuple[Path | None, str | None]:
    """Containment-check a part fixture path (issue #340 shared helper).

    The fixture must be a relative ``.stl`` path whose resolved location
    stays under the repo root's ``evals/cases/fixtures/`` directory.
    Symlinks are rejected outright (``is_symlink()`` on the path as
    given, before ``resolve()`` follows them): a symlink — in-tree or
    escaping — never passes, so this check stands on its own without
    relying on target containment. The ``../`` traversal and
    absolute-path rejections work the same way (via ``resolve()`` +
    ``is_relative_to``).
    The render worker's ``validate_part_path`` is the authoritative
    containment check at render time — this helper only guards the eval
    harness's staging and seed verification.
    Shared by ``evals/run.py``'s staging path and
    :func:`d33d.evals.case_schema.verify_seed` — one containment rule,
    one implementation.

    Returns ``(resolved_path, None)`` when the fixture is contained
    (the resolved path, so callers reuse it instead of resolving again),
    or ``(None, violation)`` otherwise.
    """
    if not fixture.strip():
        return None, "part fixture path is empty"
    if not fixture.isprintable():
        return None, "part fixture path contains control characters"
    if Path(fixture).is_absolute():
        return None, f"part fixture {fixture!r} is not a relative path under the repo root"
    if Path(fixture).suffix.lower() != ".stl":
        return (
            None,
            (
                f"part fixture {fixture!r} must be an STL file (the worker seeds "
                "part.stl); 3MF staging is out of scope for the eval harness"
            ),
        )
    root = repo_root.resolve()  # resolved once; reused for every path below
    if (root / fixture).is_symlink():
        return None, f"part fixture {fixture!r} is a symlink; symlinks are rejected outright"
    fixtures_dir = (root / "evals" / "cases" / "fixtures").resolve()
    try:
        resolved = (root / fixture).resolve()
        contained = resolved.is_relative_to(fixtures_dir)
    except (ValueError, OSError) as e:
        return None, f"cannot resolve part fixture {fixture!r} for containment check: {e}"
    if not contained:
        return (
            None,
            (
                f"part fixture {fixture!r} escapes {fixtures_dir} (the fixture "
                "must live under evals/cases/fixtures/)"
            ),
        )
    return resolved, None
