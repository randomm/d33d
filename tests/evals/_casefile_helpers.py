"""Shared helpers for the eval case-file tests (issue #340 fix round).

The case-file tests build temp case dirs with a real, pinned prompt
file. :func:`write_cases` is the single home for that scaffolding so
the test modules don't each carry a copy. Also hosts the module-level
constants and load/run helpers the ``test_import_*`` modules share.
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CASES_DIR = REPO_ROOT / "evals" / "cases"
FIXTURE = CASES_DIR / "fixtures" / "part.stl"
IMPORT_CASE_IDS = (
    "import-drill-hole",
    "import-cut-slot",
    "import-split-for-bed",
)


def fixture_extents(fixture: Path) -> tuple[float, float, float]:
    """The fixture mesh's extents in mm (shared by the import-boundary
    and case-file tests — one loader, no per-module copy)."""
    import trimesh

    assert fixture.is_file(), f"fixture missing: {fixture} does not exist"
    mesh = trimesh.load(str(fixture))
    return tuple(float(e) for e in mesh.extents)


def _load() -> dict:
    from d33d.evals.case_schema import load_golden_set

    return load_golden_set(CASES_DIR, REPO_ROOT)


def _load_run_module():
    spec = importlib.util.spec_from_file_location(
        "evals_run", REPO_ROOT / "evals" / "run.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def write_cases(tmp_path: Path, cases: list[dict]) -> Path:
    """Write case files (with a real, pinned prompt file) into a temp dir."""
    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir(exist_ok=True)
    prompt = prompts_dir / "p.md"
    prompt.write_text("# prompt v1", encoding="utf-8")
    pin = hashlib.sha256(prompt.read_bytes()).hexdigest()
    cases_dir = tmp_path / "cases"
    cases_dir.mkdir(exist_ok=True)
    for c in cases:
        doc = {
            "prompt": {
                "prompt_version": "v1",
                "path": str(prompt),
                "sha256": pin,
            },
            "gate_expectations": ["compile"],
        }
        doc.update(c)
        (cases_dir / f"{c['case_id']}.json").write_text(
            json.dumps(doc), encoding="utf-8"
        )
    return cases_dir


class _RenderResult:
    def __init__(self) -> None:
        self.error_class = "ok"
        self.stderr = ""
        self.scad_source = ""
        self.stl = None
        self.csg = None
        self.views = ()


def _llm_factory():
    """The stub LLM call. First call per run is the design call — return
    OpenSCAD. Subsequent calls are the judge — return a passing verdict
    (the stub render fn has no STL/views, so the judge's content must be
    JSON it can parse). The bad cases never reach the judge (they fail
    at staging), so the normal case's judge call is the 2nd call."""
    state = {"calls": 0}

    async def factory(request_body):
        state["calls"] += 1
        content = (
            'scale(1) import("part.stl");'
            if state["calls"] == 1
            else '{"pass": true, "reason": "stub verdict"}'
        )

        class R:
            def json(self):
                return {"choices": [{"message": {"content": content}}]}

        return R()

    return factory


def _run(tmp_path: Path, cases: list[dict], render_fn=None):
    run = _load_run_module()
    if render_fn is None:

        def render_fn(scad_source, **kwargs):
            return _RenderResult()

    return asyncio.run(
        run._run_all(
            repo_root=REPO_ROOT,
            config={},
            request_factory=_llm_factory(),
            model_id="m",
            cases_dir=write_cases(tmp_path, cases),
            render_fn=render_fn,
        )
    )
