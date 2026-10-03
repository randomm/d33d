"""Fast, non-live checks for the run.py mesh-staging path (issue #340):
the ``part_path``/``repo_dir`` seam for ``part``-carrying cases and the
per-case containment of staging problems (a staging failure is the
case's failure, never the run's)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from d33d.evals.case_schema import (
    GoldenCase,
    PromptPin,
    load_golden_set,
    verify_seed,
)
from d33d.evals.part_ref import PartRef
from tests.evals._casefile_helpers import (
    FIXTURE,
    IMPORT_CASE_IDS,
    REPO_ROOT,
    _load_run_module,
    _RenderResult,
    _run,
)
from tests.evals._casefile_helpers import write_cases as _write_cases

# ---------------------------------------------------------------------------
# (b) run.py mesh-staging: the fixture goes to the render seam as part_path
# ---------------------------------------------------------------------------


def test_run_all_stages_fixture_as_part_path(tmp_path: Path) -> None:
    """run.py hands the case's fixture to the render seam as part_path +
    repo_dir for a part-carrying case, and renders without a part for a
    part-less case."""
    run = _load_run_module()

    cases_dir = _write_cases(
        tmp_path,
        [
            {
                "case_id": "import-drill-hole",
                "kind": "imported_part",
                "request": "drill a hole",
                "part": {"fixture": "evals/cases/fixtures/part.stl", "scale": 1.0},
            },
            {
                "case_id": "primitive-box-20mm",
                "kind": "primitive",
                "request": "a box",
            },
        ],
    )

    calls: list[dict] = []

    def render_fn(scad_source, **kwargs):
        calls.append({"scad": scad_source, **kwargs})
        return _RenderResult()

    async def factory(request_body):
        class R:
            def json(self):
                return {
                    "choices": [{"message": {"content": "scale(1) import(\"part.stl\")"}}]
                }

        return R()

    report = asyncio.run(
        run._run_all(
            repo_root=REPO_ROOT,
            config={},
            request_factory=factory,
            model_id="m",
            cases_dir=cases_dir,
            render_fn=render_fn,
        )
    )
    assert report  # the report JSON is non-empty

    staged = [c for c in calls if "part_path" in c]
    unstaged = [c for c in calls if "part_path" not in c]
    assert len(staged) == 1 and len(unstaged) == 1
    assert staged[0]["part_path"] == REPO_ROOT / "evals" / "cases" / "fixtures" / "part.stl"
    assert staged[0]["repo_dir"] == REPO_ROOT


# ---------------------------------------------------------------------------
# (b2a) empty fixture path
# ---------------------------------------------------------------------------


def test_check_fixture_containment_rejects_empty_path(tmp_path: Path) -> None:
    from d33d.evals.fixtures import check_fixture_containment

    assert check_fixture_containment(tmp_path, "") == (None, "part fixture path is empty")
    assert check_fixture_containment(tmp_path, "   ") == (None, "part fixture path is empty")


def test_check_fixture_containment_rejects_control_chars(tmp_path: Path) -> None:
    from d33d.evals.fixtures import check_fixture_containment

    assert (
        check_fixture_containment(tmp_path, "a\u0000b.stl")
        == (None, "part fixture path contains control characters")
    )


# ---------------------------------------------------------------------------
# (b2) per-case containment: staging problems fail the case, never the run
# ---------------------------------------------------------------------------


def test_escaping_fixture_yields_failure_outcome_other_cases_run(tmp_path: Path):
    """A ``../`` fixture escape is that case's failure — the run, the other
    case, and the report are all unaffected (issue #340 per-case
    containment)."""
    import json as _json

    report = _run(
        tmp_path,
        [
            {
                "case_id": "escape-bad",
                "kind": "imported_part",
                "request": "drill a hole",
                "part": {
                    "fixture": "evals/cases/fixtures/../part.stl",
                    "scale": 1.0,
                },
            },
            {
                "case_id": "normal-box",
                "kind": "primitive",
                "request": "a box",
            },
        ],
    )
    report_doc = _json.loads(report)
    cases = report_doc["cases"]
    bad = cases["escape-bad"]
    assert bad["ok"] is False
    assert bad["failure_class"] == "artifact_error"
    assert "escapes" in bad["detail"]
    # the other case still ran and still passes
    good = cases["normal-box"]
    assert good["ok"] is True
    assert good["gates"]["compile"]["status"] == "pass"
    assert report_doc["summary"]["total"] == 2
    assert report_doc["summary"]["passed"] == 1


def test_non_stl_fixture_yields_failure_outcome_other_cases_run(tmp_path: Path):
    """A ``.3mf`` fixture (3MF staging is out of scope) is that case's
    failure — the run, the other case, and the report are unaffected."""
    import json as _json

    report = _run(
        tmp_path,
        [
            {
                "case_id": "bad-ext",
                "kind": "imported_part",
                "request": "drill a hole",
                "part": {
                    "fixture": "evals/cases/fixtures/part.3mf",
                    "scale": 1.0,
                },
            },
            {
                "case_id": "normal-box",
                "kind": "primitive",
                "request": "a box",
            },
        ],
    )
    report_doc = _json.loads(report)
    cases = report_doc["cases"]
    bad = cases["bad-ext"]
    assert bad["ok"] is False
    assert bad["failure_class"] == "artifact_error"
    assert "must be an STL file" in bad["detail"]
    good = cases["normal-box"]
    assert good["ok"] is True
    assert report_doc["summary"]["passed"] == 1


def test_stage_fixture_unresolvable_path_yields_error_outcome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failure in ``Path.resolve`` (an OSError from the filesystem) is
    that case's staging failure — the helper's own
    ``except (ValueError, OSError)`` branch fires and ``_stage_fixture``
    returns the genuine violation as ``(None, error)``, never an
    exception, never an aborted run."""
    run = _load_run_module()

    cases_dir = _write_cases(
        tmp_path,
        [
            {
                "case_id": "unresolvable-import",
                "kind": "imported_part",
                "request": "drill a hole",
                "part": {
                    "fixture": "evals/cases/fixtures/part.stl",
                    "scale": 1.0,
                },
            },
        ],
    )
    case = load_golden_set(cases_dir, REPO_ROOT)["unresolvable-import"]

    _real_resolve = Path.resolve

    def _resolve_proxy(self, *args, **kwargs):
        if "part.stl" in str(self):
            raise OSError("ENAMETOOLONG: path component too long")
        return _real_resolve(self, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", _resolve_proxy)
    part_path, error = run._stage_fixture(REPO_ROOT, case)
    assert part_path is None
    assert error is not None
    assert "cannot resolve part fixture" in error


def test_missing_fixture_yields_failure_outcome(tmp_path: Path):
    import json as _json

    report = _run(
        tmp_path,
        [
            {
                "case_id": "missing-fix",
                "kind": "imported_part",
                "request": "drill a hole",
                "part": {
                    "fixture": "evals/cases/fixtures/no-such.stl",
                    "scale": 1.0,
                },
            },
            {
                "case_id": "normal-box",
                "kind": "primitive",
                "request": "a box",
            },
        ],
    )
    cases = _json.loads(report)["cases"]
    bad = cases["missing-fix"]
    assert bad["ok"] is False
    assert bad["failure_class"] == "artifact_error"
    assert "missing on disk" in bad["detail"]
    assert cases["normal-box"]["ok"] is True


def test_is_file_oserror_is_a_staging_outcome_not_an_abort(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """A filesystem error from the post-staging ``is_file()`` check
    (e.g. ``PermissionError``) is the case's ``artifact_error`` staging
    outcome — it never escapes the case loop, and the normal case plus
    the report are unaffected (issue #340 staging sweep)."""
    import json as _json

    real_is_file = Path.is_file

    def _raise(self, *args, **kwargs):
        # Poison ONLY the fixture path itself: the run also stats case
        # files and the catalogue, and those must keep working.
        if self.name == "part.stl":
            raise PermissionError("permission denied")
        return real_is_file(self, *args, **kwargs)

    monkeypatch.setattr(Path, "is_file", _raise)
    report = _run(
        tmp_path,
        [
            {
                "case_id": "bad-fs-import",
                "kind": "imported_part",
                "request": "drill a hole",
                "part": {"fixture": "evals/cases/fixtures/part.stl", "scale": 1.0},
            },
            {"case_id": "normal-box", "kind": "primitive", "request": "a box"},
        ],
    )
    report_doc = _json.loads(report)
    cases = report_doc["cases"]
    bad = cases["bad-fs-import"]
    assert bad["ok"] is False
    assert bad["failure_class"] == "artifact_error"
    assert "fixture staging" in bad["detail"]
    good = cases["normal-box"]
    assert good["ok"] is True
    assert good["gates"]["compile"]["status"] == "pass"
    assert report_doc["summary"]["total"] == 2
    assert report_doc["summary"]["passed"] == 1


def test_check_fixture_containment_rejects_symlink_outright(tmp_path: Path):
    """A symlinked fixture is rejected outright (before ``resolve()``
    follows it) — even when the target stays inside
    ``evals/cases/fixtures/``."""
    from d33d.evals.fixtures import check_fixture_containment

    fixtures_dir = tmp_path / "evals" / "cases" / "fixtures"
    fixtures_dir.mkdir(parents=True)
    target = fixtures_dir / "real.stl"
    target.write_bytes(b"stl")
    link = fixtures_dir / "linked.stl"
    link.symlink_to(target)

    resolved, violation = check_fixture_containment(tmp_path, "evals/cases/fixtures/linked.stl")
    assert resolved is None
    assert violation is not None
    assert "symlink" in violation
    # a regular file in the same directory still passes
    resolved, violation = check_fixture_containment(
        tmp_path, "evals/cases/fixtures/real.stl"
    )
    assert violation is None and resolved is not None


# ---------------------------------------------------------------------------
# (b3) verify_seed containment via the shared helper (issue #340 fix round)
# ---------------------------------------------------------------------------


def test_verify_seed_flags_escaping_fixture() -> None:
    """A fixture that escapes ``evals/cases/fixtures/`` (``../``
    traversal) is a seed violation, reported via the shared containment
    helper — not just a missing file."""
    cases = load_golden_set(REPO_ROOT / "evals" / "cases", REPO_ROOT)
    cases[IMPORT_CASE_IDS[0]].part.fixture = "evals/cases/fixtures/../part.stl"
    violations = verify_seed(cases, REPO_ROOT)
    assert any("escapes" in v for v in violations)


def test_verify_seed_flags_absolute_fixture(tmp_path: Path) -> None:
    """An absolute fixture path is a seed violation (the fixture must be a
    relative path under the repo root)."""
    cases = load_golden_set(REPO_ROOT / "evals" / "cases", REPO_ROOT)
    cases[IMPORT_CASE_IDS[0]].part.fixture = str(FIXTURE)
    violations = verify_seed(cases, REPO_ROOT)
    assert any("not a relative path" in v for v in violations)


def test_part_of_returns_part_ref_for_imported_case() -> None:
    """``part_of`` returns ``case.part`` for an imported-part case and
    raises ``ValueError`` (not ``AttributeError``) when the ref is absent
    (the schema makes this unreachable, but the accessor must stay
    honest)."""
    from d33d.evals.case_schema import load_golden_set
    from d33d.evals.part_ref import part_of

    cases = load_golden_set(REPO_ROOT / "evals" / "cases", REPO_ROOT)
    case = cases[IMPORT_CASE_IDS[0]]
    part = part_of(case)
    assert part is case.part
    assert part.fixture == "evals/cases/fixtures/part.stl"

    ghost = GoldenCase(
        case_id="ghost-no-part",
        kind="primitive",
        prompt=PromptPin(prompt_version="v1", path="p.md", sha256="f" * 64),
        request="a box",
        gate_expectations=["compile"],
    )
    assert ghost.part is None
    with pytest.raises(ValueError, match="no part ref"):
        part_of(ghost)


def test_part_ref_scale_rejects_non_finite() -> None:
    """A non-finite ``scale`` (inf/nan) is a schema error — the guard's
    numeric tolerance must never compare against a non-finite factor."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="finite"):
        PartRef(fixture="evals/cases/fixtures/part.stl", scale=float("inf"))
    # NaN is caught earlier by the ``gt=0`` constraint (NaN < 0 is
    # False), but the error is still a ValidationError.
    with pytest.raises(ValidationError):
        PartRef(fixture="evals/cases/fixtures/part.stl", scale=float("nan"))
    # a normal positive scale still validates
    assert PartRef(fixture="evals/cases/fixtures/part.stl", scale=1.0).scale == 1.0


def test_verify_seed_without_repo_root_reports_cannot_verify() -> None:
    """With ``repo_root=None`` the containment check cannot resolve the
    fixture path: the violation is "cannot verify fixture without
    repo_root" (a None repo_root is not dereferenced) and the run of
    the other checks still completes without crashing."""
    cases = load_golden_set(REPO_ROOT / "evals" / "cases", REPO_ROOT)
    violations = verify_seed(cases)
    for cid in IMPORT_CASE_IDS:
        assert any(
            v.startswith(f"{cid}: cannot verify fixture without repo_root")
            for v in violations
        ), f"{cid}: missing the no-repo_root violation in {violations}"
