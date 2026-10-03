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
import sys
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
    # ``evals/run.py`` inserts the repo root into ``sys.path`` at import
    # time (so the standalone CLI can ``import d33d``).  Under pytest the
    # repo root is already on the path, so the insert is a no-op in the
    # common case — but a stray ``sys.path`` mutation that survives into
    # later tests can change module resolution and (on some runners)
    # leave the default ``ThreadPoolExecutor`` in a state where the
    # product's off-the-loop ``to_thread`` timing test sees a blocked
    # worker.  Snapshot and restore ``sys.path`` so the mutation is
    # contained to this module's import.
    saved_path = list(sys.path)
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.path[:] = saved_path
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
    """The stub LLM call, keyed on the REQUEST, never on call order.

    The judge request's SYSTEM message is ``d33d.evals.judge.JUDGE_PROMPT``
    (a fixed constant), while the design request's system message is the
    case's hash-pinned prompt file. Discriminating on the system prompt
    means a normal case placed ANYWHERE in the set still gets the design
    response for its design call — the previous call counter made the
    design-vs-judge decision depend on case order.

    The judge gets a passing verdict (the stub render fn has no
    STL/views, so the judge's content must be JSON it can parse)."""

    from d33d.evals.judge import JUDGE_PROMPT

    async def factory(request_body):
        system = request_body["messages"][0]["content"]
        is_judge = system == JUDGE_PROMPT
        content = (
            '{"pass": true, "reason": "stub verdict"}'
            if is_judge
            else 'scale(1) import("part.stl");'
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
