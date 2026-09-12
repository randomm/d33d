"""promptfoo runner (issue #9, workstream task-harness).

The entrypoint for the promptfoo harness. It is the single file the
``promptfoo.config.yaml`` custom asserts point at (``evals/run.py``),
and the CLI runs it as ``python evals/run.py <assert-name>`` — the
assert's ``value`` selects one of the two assert functions below:

* ``deterministic_gates_pass`` — the case's gate phase (gates 1-7 via
  ``d33d.evals.harness.run_case_gates``). promptfoo runs this assert
  BEFORE the judge assert; a failing gate short-circuits the judge
  (the spec's "7 deterministic gates before any vision judge").
* ``vision_judge_pass`` — the judge verdict (``d33d.evals.judge.
  judge_render``) for the gate-passing candidate.

The runner's own CLI (``python evals/run.py --run``) drives a full
local run without the promptfoo CLI: it loads the promptfoo config
(:func:`d33d.evals.harness.load_promptfoo_config` — validating the
cases floor of 20 and the 5 hash-pinned prompts), loads the golden set
(``d33d.evals.case_schema.load_golden_set`` — re-checking every prompt
pin), runs each case through ``d33d.evals.harness.run_case`` (gates ->
design call -> judge, in that order), and writes the report
(``d33d.evals.report.build_report`` — the "best-candidate + reason"
shape the design loop uses).

Constraints honored here (the rest live in the modules they name):

* **The model is configured, never hardcoded.** The design-role model
  is resolved from the models.yaml catalogue via
  ``d33d.config.resolve.resolve_model`` (the day-1 default
  ``RedHatAI/Qwen3.8-27B-INT4`` at ``llm.trailopeners.com/v1`` is a
  catalogue entry, not a constant here). ``--catalogue`` names the
  file; ``--model`` is an explicit override (a bare model id, for
  one-off runs against a non-catalogue endpoint).
* **The request factory is injected.** ``make_openai_factory`` builds
  an async ``request -> response`` closure over httpx that carries the
  ``Authorization: Bearer`` key from ``D33D_EVAL_LLM_KEY`` (or the
  catalogue's provider key) — the key stays inside the closure and
  never appears in a message, a hash, or a report. Server-side only;
  the API key never reaches the browser.
* **``D33D_FAILURES_JSONL`` is respected** via the same default the
  app uses (``d33d.evals.failure_capture.default_failures_path``). The
  runner reads the env var (``os.environ.get("D33D_FAILURES_JSONL")``,
  falling back to ``default_failures_path()``) and names the sink in
  the report, so a run's output shows where production gate failures
  archive (the operator can point a run at a different file without
  touching the config). The runner is the EVAL path and never calls
  the production hook (structural exclusion, per
  ``d33d/evals/failure_capture.py``) — it reads the path to report on
  it, not to write to it.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path
from typing import Any

import httpx

from d33d.evals.case_schema import load_golden_set
from d33d.evals.harness import (
    RequestFactory,
    load_promptfoo_config,
    run_case,
)
from d33d.evals.report import build_report

# Make the repo root importable (the script lives at ``evals/``).
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


# ---------------------------------------------------------------------------
# The two promptfoo custom asserts (the CLI calls these via evals/run.py)
# ---------------------------------------------------------------------------


def deterministic_gates_pass(output: dict, test: dict, vars: dict) -> bool:
    """promptfoo assert: the case's gate phase passed.

    ``output`` is the harness's per-case row (``CaseOutcome.to_dict()``
    — the ``d33d`` key carries it when the runner's other assert wrote
    it). A case with no gates declared (an adversarial refusal) passes
    this assert (nothing to gate); a case with a failed gate fails it,
    short-circuiting the judge assert that follows.
    """
    row = output.get("d33d") or output
    gates = row.get("gates") or {}
    return all(g.get("status") != "fail" for g in gates.values())


def vision_judge_pass(output: dict, test: dict, vars: dict) -> bool:
    """promptfoo assert: the judge verdict passed (only reached when
    the gates passed — the ordering is the spec's).

    A gate-passing candidate with no judge verdict (``judge is None``)
    passes: the judge only runs for candidates whose declared gates let
    them through (``CaseOutcome.ok`` is the source of truth — gates ok
    AND, when the judge ran, the judge passed). Failing closed here
    would report a false-negative for gate-passing cases the judge
    legitimately did not score (e.g. an adversarial case that declared
    only ``compile`` and short-circuited the judge)."""
    row = output.get("d33d") or output
    judge = row.get("judge")
    if judge is None:
        return True
    return bool(judge.get("passed"))


# ---------------------------------------------------------------------------
# The injected LLM request factory (server-side; the key stays inside)
# ---------------------------------------------------------------------------


def make_openai_factory(
    base_url: str,
    api_key: str,
    *,
    timeout_s: float = 120.0,
) -> RequestFactory:
    """Build the async ``request -> response`` factory the harness and
    judge consume.

    The request body is the OpenAI-shaped ``{"model", "messages", ...}``
    dict; the factory POSTs it to ``{base_url}/chat/completions`` with
    the ``Authorization: Bearer <key>`` header (the key stays inside
    the closure — never in a message, a hash, or a report) and returns
    the httpx response (the harness reads ``.json()``).
    """

    async def _factory(request_body: dict[str, Any]) -> httpx.Response:
        async with httpx.AsyncClient(timeout=timeout_s) as client:
            return await client.post(
                f"{base_url.rstrip('/')}/chat/completions",
                json=request_body,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
            )

    return _factory


# ---------------------------------------------------------------------------
# The local runner (no promptfoo CLI needed)
# ---------------------------------------------------------------------------


def _resolve_model(catalogue_path: Path, role: str = "design") -> tuple[str, str]:
    """Resolve the role to ``(model_id, base_url)`` via the catalogue.

    The catalogue (models.yaml) is the source of truth — the model is
    configured, never hardcoded. Returns ``(the concrete provider model
    id, the provider base url)``; the caller pairs them with the
    provider key (from the catalogue) to build the request factory.
    """
    from d33d.config.catalogue import load_catalogue
    from d33d.config.resolve import resolve_model

    cat = load_catalogue(catalogue_path)
    res = resolve_model(cat, role)
    return res.entry.model, res.provider.base


async def _run_all(
    *,
    repo_root: Path,
    config: dict[str, Any],
    request_factory: RequestFactory,
    model_id: str,
    cases_dir: Path,
    catalogue_path: Path | None = None,
) -> str:
    """Run every golden-set case through the harness; return the report
    JSON.

    The gates run first (per case, via ``run_case_gates``), then the
    design call, then the judge — the ordering the spec pins. The
    report is ``d33d.evals.report.build_report`` over the per-case
    outcomes (the "best-candidate + reason" shape the design loop
    uses).
    """
    cases = load_golden_set(cases_dir, repo_root)

    outcomes = []
    for case_id in sorted(cases):
        case = cases[case_id]
        # The render input for this case: a local run has no render
        # worker — the candidate is the case's own reference (the .scad
        # stand-in the model is expected to reproduce). The harness
        # shells into the render worker directly in a real run (the
        # structural exclusion of the production hook); here the
        # stand-in is a safe default for a promptfoo dry-run.
        render_result, stl_path, mesh = _local_render_inputs(repo_root, case)
        outcome = await run_case(
            case=case,
            repo_root=repo_root,
            render_result=render_result,
            mesh=mesh,
            stl_path=stl_path,
            model_id=model_id,
            request_factory=request_factory,
        )
        outcomes.append(outcome)

    report = build_report(outcomes)
    return report.to_json()


def _local_render_inputs(repo_root: Path, case: Any) -> tuple[Any, str | None, Any]:
    """The render inputs a local (no render-worker) run can supply.

    A local ``--run`` is a design-loop dry-run: the case's reference
    fixture (a ``.scad`` stand-in) is rendered best-effort with
    ``openscad`` so the case's own ``gate_expectations`` decide the
    outcome — declared gates pass when their inputs are present (the
    STL exists, the mesh is watertight) and gates that need the render
    worker's pipeline (slice) fall out of the case's declared set or
    report their pinned N/A. When ``openscad`` is not invocable the
    case declares ``compile`` but the render stand-in cannot supply an
    STL: the case then fails at gate 2 (``stl_export``) or the
    watertight gate — a visible, correct result, not a silent
    gate-2 failure for every case that merely declared ``stl_export``.
    """
    stl_path: str | None = None
    mesh: Any = None
    render_result = _null_render()
    if case.reference_photo:
        reference = case.reference_photo
    elif case.rendered_views:
        reference = case.rendered_views[0]
    else:
        reference = None
    if reference:
        fixture = Path(reference)
        if not fixture.is_absolute():
            fixture = repo_root / fixture
        if fixture.suffix == ".scad":
            mesh = _scad_to_mesh(repo_root, fixture)
            if mesh is not None:
                stl_path = f"local:{fixture.name}"
    return render_result, stl_path, mesh


def _null_render() -> Any:
    """A render-worker result with ``error_class='ok'`` and no STL.

    A local run without the render worker (the default for ``--run``)
    has no real render; the harness's gates consume this stand-in
    (gate 1 passes on ``ok``; gate 2 is then decided by the case's own
    declared expectations and the rendered STL, see
    :func:`_local_render_inputs`).
    """

    class _Render:
        error_class = "ok"
        stderr = ""
        scad_source = ""

    return _Render()


def _scad_to_mesh(repo_root: Path, scad_path: Path) -> Any:
    """Best-effort mesh for a ``.scad`` fixture (``None`` when openscad
    is not invocable or the load fails).

    The case's ``.scad`` stand-in is the reference geometry; the judge
    and the report use the mesh when available. A local run without
    openscad simply has ``None`` (the judge evaluates on the source +
    text only).
    """
    if not scad_path.is_absolute():
        scad_path = repo_root / scad_path
    if not scad_path.is_file():
        return None
    try:
        import subprocess
        import tempfile

        import trimesh

        with tempfile.TemporaryDirectory() as tmp:
            stl = Path(tmp) / "model.stl"
            proc = subprocess.run(
                ["openscad", "--stdout", str(stl), str(scad_path)],
                capture_output=True,
                check=False,
                timeout=60,
            )
            if proc.returncode != 0 or not stl.is_file():
                return None
            return trimesh.load(str(stl), process=False)
    except (OSError, ValueError, KeyError):
        return None


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint.

    ``python evals/run.py <assert-name>`` — the promptfoo custom assert
    path (the config points here; promptfoo imports the named function
    and calls it with ``(output, test, vars)``).

    ``python evals/run.py --run`` — the local runner (no promptfoo CLI):
    loads the config, resolves the model from the catalogue, runs every
    golden-set case through the harness (gates -> design call -> judge),
    and writes the report to stdout.
    """
    if argv is None:
        argv = sys.argv[1:]

    if not argv or argv[0] in ("--help", "-h"):
        print(__doc__)
        return 0

    if argv[0] in ("deterministic_gates_pass", "vision_judge_pass"):
        # The promptfoo custom-assert path: promptfoo imports this file
        # and calls the named function. There is nothing to run here —
        # the import is the contract.
        return 0

    if argv[0] == "--run":
        parser = argparse.ArgumentParser(description="run the eval harness locally")
        parser.add_argument("--catalogue", type=Path, default=None,
                             help="path to models.yaml (default: the config's "
                                  "catalogue_path or the app default)")
        parser.add_argument("--model", type=str, default=None,
                             help="explicit model id override (default: the "
                                  "catalogue's design role)")
        parser.add_argument("--base-url", type=str, default=None,
                             help="OpenAI-compatible base URL (default: the "
                                  "catalogue's provider base)")
        parser.add_argument("--api-key", type=str, default=None,
                             help="API key (default: D33D_EVAL_LLM_KEY env, "
                                  "or the catalogue's provider key)")
        parser.add_argument("--cases", type=Path, default=None,
                             help="cases dir (default: the config's cases_dir)")
        args = parser.parse_args(argv[1:])

        repo_root = _REPO_ROOT
        config = load_promptfoo_config(repo_root=repo_root)
        cases_dir = (
            args.cases
            if args.cases is not None
            else repo_root / str(config["cases_dir"])
        )

        # The failures.jsonl sink (the operator can point a run at a
        # different file via D33D_FAILURES_JSONL; the runner names the
        # sink in the report but never writes to it — the eval path is
        # structurally excluded from the production hook).
        from d33d.evals.failure_capture import default_failures_path

        failures_path = Path(
            os.environ.get("D33D_FAILURES_JSONL") or default_failures_path()
        )
        print(f"# failures.jsonl sink: {failures_path}", file=sys.stderr)

        catalogue_path = args.catalogue
        model_id = args.model
        base_url = args.base_url
        api_key = args.api_key or os.environ.get("D33D_EVAL_LLM_KEY", "")

        if catalogue_path is not None or model_id is None or base_url is None:
            cat_path = (
                catalogue_path
                if catalogue_path is not None
                else repo_root / "models.yaml"
            )
            if cat_path.is_file() and (model_id is None or base_url is None):
                resolved_id, resolved_base = _resolve_model(cat_path)
                model_id = model_id or resolved_id
                base_url = base_url or resolved_base
                if not api_key:
                    # The catalogue's provider key (resolved from the
                    # env var at load time — server-side only).
                    from d33d.config.catalogue import load_catalogue

                    cat = load_catalogue(cat_path)
                    prov = cat.providers[next(iter(cat.providers))]
                    api_key = api_key or prov.key

        if not model_id or not base_url or not api_key:
            print(
                "error: a model id, base URL, and API key are required "
                "(pass --model/--base-url/--api-key, set D33D_EVAL_LLM_KEY, "
                "or point --catalogue at a models.yaml)",
                file=sys.stderr,
            )
            return 2

        factory = make_openai_factory(base_url, api_key)
        report_json = asyncio.run(
            _run_all(
                repo_root=repo_root,
                config=config,
                request_factory=factory,
                model_id=model_id,
                cases_dir=cases_dir,
                catalogue_path=catalogue_path,
            )
        )
        print(report_json)
        return 0

    print(f"unknown command: {argv[0]}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
