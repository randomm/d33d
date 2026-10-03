"""Part-fixture path containment for the eval harness (issue #340).

The imported-part cases (and the run.py staging path) reference a mesh
fixture under ``evals/cases/fixtures/``. The path is user-supplied case
data, so it must be contained in that directory: a ``../`` traversal, an
absolute path, or a symlink escape is a per-case violation, never a
crash. This module is the single home for that rule.
"""

from __future__ import annotations

from pathlib import Path


def check_fixture_containment(repo_root: Path, fixture: str) -> str | None:
    """Containment-check a part fixture path (issue #340 shared helper).

    The fixture must be a relative ``.stl`` path whose resolved location
    stays under the repo root's ``evals/cases/fixtures/`` directory.
    Symlinks: an in-tree symlink (target inside ``evals/cases/fixtures/``)
    passes here, because ``resolve()`` follows it and the resolved target
    is still contained; a symlink whose target escapes that directory is
    rejected by the same ``resolve()`` + ``is_relative_to`` check (the
    ``../`` traversal and absolute-path rejections work the same way).
    The render worker's ``validate_part_path`` is the authoritative
    containment check at render time — this helper only guards the eval
    harness's staging and seed verification.
    Shared by ``evals/run.py``'s staging path and
    :func:`d33d.evals.case_schema.verify_seed` — one containment rule,
    one implementation.

    Returns ``None`` when the fixture is contained, or the violation
    message otherwise.
    """
    if not fixture.strip():
        return "part fixture path is empty"
    if Path(fixture).is_absolute():
        return f"part fixture {fixture!r} is not a relative path under the repo root"
    if Path(fixture).suffix.lower() != ".stl":
        return (
            f"part fixture {fixture!r} must be an STL file (the worker seeds "
            "part.stl); 3MF staging is out of scope for the eval harness"
        )
    root = repo_root.resolve()
    fixtures_dir = (root / "evals" / "cases" / "fixtures").resolve()
    try:
        resolved = (root / fixture).resolve()
        contained = resolved.is_relative_to(fixtures_dir)
    except (ValueError, OSError) as e:
        return f"cannot resolve part fixture {fixture!r} for containment check: {e}"
    if not contained:
        return (
            f"part fixture {fixture!r} escapes {fixtures_dir} (the fixture "
            "must live under evals/cases/fixtures/)"
        )
    return None
