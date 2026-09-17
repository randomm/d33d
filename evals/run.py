"""promptfoo runner (issue #9, workstream task-harness).

The entrypoint for the promptfoo harness. It is the single file the
``promptfoo.config.yaml`` custom asserts point at (``evals/run.py``),
and the CLI runs it as ``python evals/run.py <assert-name>`` — the
assert's ``value`` selects one of the two assert functions below:

* ``deterministic_gates_pass`` — the case's gate phase (gates 1-7 via
  ``d33d.evals.harness.run_case_gates``) run on the MODEL'S OUTPUT,
  rendered through the injected render worker. promptfoo runs this
  assert BEFORE the judge assert; a failing gate short-circuits the
  judge (the spec's "7 deterministic gates before any vision judge").
* ``vision_judge_pass`` — the judge verdict (``d33d.evals.judge.
  judge_render``) for the gate-passing candidate.

The runner's own CLI (``python evals/run.py --run``) drives a full
local run without the promptfoo CLI: it loads the promptfoo config
(:func:`d33d.evals.harness.load_promptfoo_config` — validating the
cases floor of 20 and the 5 hash-pinned prompts), loads the golden set
(``d33d.evals.case_schema.load_golden_set`` — re-checking every prompt
pin), runs each case through ``d33d.evals.harness.run_case`` (design
call -> real render of the model's output -> gates on that output ->
judge, in that order — issue #108), and writes the report
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
    RenderFn,
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
    render_fn: RenderFn,
) -> str:
    """Run every golden-set case through the harness; return the report
    JSON.

    Per case, the harness (issue #108 ordering) runs the design call
    FIRST, then the injected ``render_fn`` renders the MODEL'S OUTPUT
    through the render worker, then the gates run on that rendered
    output, then the judge. The report is
    ``d33d.evals.report.build_report`` over the per-case outcomes (the
    "best-candidate + reason" shape the design loop uses).
    """
    cases = load_golden_set(cases_dir, repo_root)

    outcomes = []
    for case_id in sorted(cases):
        case = cases[case_id]
        outcome = await run_case(
            case=case,
            repo_root=repo_root,
            model_id=model_id,
            request_factory=request_factory,
            render_fn=render_fn,
        )
        outcomes.append(outcome)

    report = build_report(outcomes)
    return report.to_json()


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint.

    ``python evals/run.py <assert-name>`` — the promptfoo custom assert
    path (the config points here; promptfoo imports the named function
    and calls it with ``(output, test, vars)``).

    ``python evals/run.py --run`` — the local runner (no promptfoo CLI):
    loads the config, resolves the model from the catalogue, and runs
    every golden-set case through the harness (design call -> real
    render of the model's output via the injected render worker -> gates
    -> judge) — then writes the report to stdout.
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
        parser.add_argument(
            "--catalogue",
            type=Path,
            default=None,
            help="path to models.yaml (default: the config's "
            "catalogue_path or the app default)",
        )
        parser.add_argument(
            "--model",
            type=str,
            default=None,
            help="explicit model id override (default: the catalogue's design role)",
        )
        parser.add_argument(
            "--base-url",
            type=str,
            default=None,
            help="OpenAI-compatible base URL (default: the catalogue's provider base)",
        )
        parser.add_argument(
            "--api-key",
            type=str,
            default=None,
            help="API key (default: D33D_EVAL_LLM_KEY env, "
            "or the catalogue's provider key)",
        )
        parser.add_argument(
            "--cases",
            type=Path,
            default=None,
            help="cases dir (default: the config's cases_dir)",
        )
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
        # The render seam (issue #108): the REAL Docker render worker —
        # the harness's gates run on the model's rendered output, never
        # on a reference fixture. The worker's temp/persist dirs live
        # under $HOME (Docker on macOS cannot see /tmp — a /tmp render
        # dir causes a silent multi-minute hang).
        from d33d.render_worker import render_for_design_loop

        render_fn: RenderFn = render_for_design_loop

        report_json = asyncio.run(
            _run_all(
                repo_root=repo_root,
                config=config,
                request_factory=factory,
                model_id=model_id,
                cases_dir=cases_dir,
                render_fn=render_fn,
            )
        )
        print(report_json)
        return 0

    print(f"unknown command: {argv[0]}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
