"""Eval harness core (issue #9, workstream task-harness).

The deterministic part of the promptfoo harness. It is the single place
the ``promptfoo.config.yaml`` custom asserts shell into (and the unit
tests call directly), and it owns the one ordering invariant the ticket
pins: **the seven deterministic gates run BEFORE any vision judge.**

Pipeline, per case:

1. **Gates 1-7** via :func:`run_case_gates` — delegates to
   ``d33d.evals.gates`` (gates 1-5), ``slice_gate`` (gate 6) and
   ``region_gate`` (gate 7). Gates short-circuit on the first failure,
   so a case that fails gate 1 never reaches gate 2 (or the judge).
2. **The design call** — the case's hash-pinned prompt file content is
   the system prompt; the case's request (+ reference photo / rendered
   views as ``image_url`` parts, base64 ``data:`` URIs when present on
   disk, a text reference when the fixture is a ``.scad`` stand-in) is
   the user turn. The LLM call is injected — the harness never opens its
   own connection and never reads an API key itself.
3. **The judge** — :func:`d33d.evals.judge.judge_render` — runs ONLY on
   a candidate that passed every applicable gate (a gate failure
   short-circuits the judge; that is the spec's ordering, enforced
   structurally, not by a flag).
4. **The report** — :func:`d33d.evals.report.build_report` aggregates
   the per-case results into the run report (including
   :func:`d33d.evals.report.select_best_candidate`, the
   "best-candidate + reason" shape the design loop uses).

Two constraints that shape this module:

* **The model is configured, never hardcoded.** The harness does not
  name a model id; the caller resolves the ``design`` role via
  ``d33d.config.resolve.resolve_model`` (the models.yaml catalogue is
  the source of truth) and passes the resolved id + request factory to
  :func:`run_case`. The day-1 default (``RedHatAI/Qwen3.8-27B-INT4`` at
  ``https://llm.trailopeners.com/v1``, key ``TRAIL_OPENERS_LLM_KEY``)
  lives in the catalogue, not here.
* **API keys never reach the browser.** The harness is server-side
  Python with no client surface; the injected ``request_factory``
  (``request -> response``) is the same seam the design loop's
  ``llm_fn`` uses (``d33d.design_llm.send``), so the key stays inside
  the factory (the ``Authorization`` header) and never appears in a
  message, a hash, or a report.

Adversarial cases are scored against the judge's outcome class
(``graceful_refusal`` / ``clearance_applied``), never as compile
failures: the judge compares the design call's output against the
case's ``adversarial.expected_outcome``.

Gate 7 (region containment) needs the pre-edit and post-edit meshes:
the post mesh comes from the case's output (``load_fn``), the pre mesh
from ``pre_mesh_fn`` (the baseline). Neither can be resolved from case
data alone (the baseline ``.scad`` needs a render), so both are
injected — when the caller cannot supply them, gate 7 reports its
pinned N/A string (``"N/A, containment convention not available"``)
via :func:`d33d.evals.region_gate.run_region_gate` — neither a pass
nor a hard fail.
"""

from __future__ import annotations

import base64
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from d33d.evals import gates as _gates
from d33d.evals.case_schema import GoldenCase
from d33d.evals.gates import GateResult
from d33d.evals.judge import JudgeInput, JudgeVerdict, judge_render
from d33d.evals.region_gate import run_region_gate
from d33d.evals.slice_gate import run_slice_gate

#: The default promptfoo config location (relative to the repo root).
DEFAULT_CONFIG_PATH = Path("evals") / "promptfoo.config.yaml"

#: The LLM request seam (identical in shape to the design loop's
#: ``request_factory``): one JSON request body -> one OpenAI-shaped
#: response. The API key stays inside the factory (its
#: ``Authorization`` header) — never in a message or a report.
RequestFactory = Callable[[dict[str, Any]], Awaitable[Any]]


@dataclass(frozen=True)
class CaseOutcome:
    """The harness's per-case result — one row of the run report.

    ``gates`` maps the gate labels to :class:`GateResult` (absent gates
    were not reached — a short-circuit). ``scad_source`` is the design
    call's raw output (``""`` when the gates short-circuited it).
    ``judge`` is set only when the gate phase let the candidate through
    (structural enforcement of gates-before-judge). ``failure_class`` is
    the closed-enum class the case failed with (``None`` when the case
    passed).
    """

    case_id: str
    kind: str
    prompt_version: str
    prompt_sha256: str
    request: str
    scad_source: str = ""
    gates: dict[str, GateResult] = field(default_factory=dict)
    judge: JudgeVerdict | None = None
    failure_class: str | None = None
    detail: str = ""

    @property
    def gates_ok(self) -> bool:
        """True iff every present gate passed (``na`` is not a failure)."""
        return all(g.status != "fail" for g in self.gates.values())

    @property
    def ok(self) -> bool:
        """The case's overall verdict: gates ok AND (when the judge ran)
        the judge passed."""
        if not self.gates_ok:
            return False
        if self.judge is None:
            return True
        return self.judge.passed

    def to_dict(self) -> dict[str, Any]:
        """JSON-serialisable row for the run report."""
        return {
            "case_id": self.case_id,
            "kind": self.kind,
            "prompt_version": self.prompt_version,
            "prompt_sha256": self.prompt_sha256,
            "request": self.request,
            "scad_source": self.scad_source,
            "gates": {name: g.to_dict() for name, g in self.gates.items()},
            "judge": self.judge.to_dict() if self.judge is not None else None,
            "failure_class": self.failure_class,
            "detail": self.detail,
            "ok": self.ok,
        }


# ---------------------------------------------------------------------------
# Promptfoo config — the single YAML the CLI runs
# ---------------------------------------------------------------------------


def load_promptfoo_config(
    config_path: str | Path | None = None,
    repo_root: str | Path | None = None,
) -> dict[str, Any]:
    """Load and structurally validate the promptfoo config.

    The config is the harness's contract with the CLI: it must name an
    OpenAI-compatible provider (``openai:completions``; the ``id`` is an
    ``{{process.env.*}}`` reference — the model is configured via the
    catalogue/env, never hardcoded), a ``cases_dir`` holding at least
    20 case files, a ``prompts_dir`` holding at least one hash-pinned
    prompt, and python custom asserts through ``evals/run.py`` with the
    deterministic-gates assert listed before the judge (the spec's
    "7 deterministic gates before any vision judge").

    Any violation raises :class:`ValueError` so a broken config fails
    loudly at run start, not mid-suite.
    """
    root = Path(repo_root) if repo_root is not None else Path.cwd()
    path = Path(config_path) if config_path is not None else root / DEFAULT_CONFIG_PATH
    if not path.is_file():
        raise FileNotFoundError(f"promptfoo config not found: {path}")
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise ValueError(f"promptfoo config {path} is not valid YAML: {e}") from e
    if not isinstance(doc, dict):
        raise TypeError(f"promptfoo config {path}: top level must be a mapping")

    providers = doc.get("providers")
    if not isinstance(providers, list) or not providers:
        raise ValueError(f"promptfoo config {path}: 'providers' must be a non-empty list")
    for provider in providers:
        if not isinstance(provider, dict):
            raise TypeError(f"promptfoo config {path}: every provider must be a mapping")
        provider_type = provider.get("provider") or provider.get("type")
        if not isinstance(provider_type, str) or not provider_type.startswith("openai"):
            raise ValueError(
                f"promptfoo config {path}: provider must be an OpenAI-compatible "
                f"endpoint ('openai:completions' or 'openai'), got {provider_type!r}"
            )
        provider_id = provider.get("id")
        if not isinstance(provider_id, str) or not provider_id:
            raise ValueError(
                f"promptfoo config {path}: provider 'id' must be a non-empty string "
                "(the model is configured via the catalogue/env, never hardcoded)"
            )

    cases_dir = doc.get("cases_dir")
    if not isinstance(cases_dir, str) or not cases_dir:
        raise ValueError(f"promptfoo config {path}: 'cases_dir' must be a non-empty string")
    resolved_cases = Path(cases_dir) if Path(cases_dir).is_absolute() else root / cases_dir
    case_files = sorted(resolved_cases.glob("*.json"))
    if len(case_files) < 20:
        raise ValueError(
            f"promptfoo config {path}: cases_dir {resolved_cases} holds "
            f"{len(case_files)} cases, the golden-set floor is 20"
        )

    prompts_dir = doc.get("prompts_dir")
    if not isinstance(prompts_dir, str) or not prompts_dir:
        raise ValueError(f"promptfoo config {path}: 'prompts_dir' must be a non-empty string")
    resolved_prompts = Path(prompts_dir) if Path(prompts_dir).is_absolute() else root / prompts_dir
    prompt_files = sorted(resolved_prompts.glob("*.md"))
    if not prompt_files:
        raise ValueError(
            f"promptfoo config {path}: prompts_dir {resolved_prompts} holds no prompt files"
        )

    defaults = doc.get("defaults")
    if not isinstance(defaults, dict):
        raise TypeError(f"promptfoo config {path}: 'defaults' must be a mapping")
    asserts = defaults.get("assert")
    if not isinstance(asserts, list) or not asserts:
        raise ValueError(f"promptfoo config {path}: 'defaults.assert' must be a non-empty list")
    for item in asserts:
        if not isinstance(item, dict) or item.get("type") != "python":
            raise ValueError(
                f"promptfoo config {path}: every default assert must be a python assert "
                f"(type: python), got {item!r}"
            )
        if item.get("path") != "evals/run.py":
            raise ValueError(
                f"promptfoo config {path}: default python asserts must point at "
                f"evals/run.py (the harness entrypoint), got {item.get('path')!r}"
            )
    if not any(item.get("value") == "deterministic_gates_pass" for item in asserts):
        raise ValueError(
            f"promptfoo config {path}: the deterministic-gates assert "
            f"('deterministic_gates_pass') must be present — the 7 gates run "
            f"before any vision judge"
        )

    return doc


# ---------------------------------------------------------------------------
# Message assembly
# ---------------------------------------------------------------------------


def _image_part(repo_root: Path, rel_path: str) -> dict[str, Any] | None:
    """One ``image_url`` content part for an on-disk image.

    ``None`` when the file is absent or not an image: photo cases whose
    fixture is a ``.scad`` stand-in (a source file, not an image) fall
    back to a text reference so the request stays well-formed without
    pretending a non-image is an image.
    """
    full = repo_root / rel_path
    if not full.is_file():
        return None
    if full.suffix.lower() not in (".png", ".jpg", ".jpeg", ".webp", ".gif"):
        return None
    encoded = base64.b64encode(full.read_bytes()).decode("ascii")
    mime = full.suffix.lower().lstrip(".")
    return {
        "type": "image_url",
        "image_url": {"url": f"data:{mime};base64,{encoded}"},
    }


def _text_reference(rel_path: str) -> dict[str, Any]:
    """The text fallback for a non-image reference (a ``.scad`` stand-in)."""
    return {
        "type": "text",
        "text": f"Reference source (provided as text): {rel_path}",
    }


def case_messages(
    case: GoldenCase, repo_root: Path, *, prompt_text: str | None = None
) -> tuple[str, list[dict[str, Any]]]:
    """The case's request + references as ``(system, messages)``.

    The system prompt is the hash-pinned prompt file's content (the case
    pins it by SHA-256; the runner re-checks the pin before calling).
    The user turn is the request text plus one part per reference photo
    / rendered view (base64 ``image_url`` part when the file is an
    on-disk image, a text reference otherwise).
    """
    system = prompt_text if prompt_text is not None else (
        repo_root / case.prompt.path
    ).read_text(encoding="utf-8")
    parts: list[dict[str, Any]] = [{"type": "text", "text": case.request}]
    if case.reference_photo:
        part = _image_part(repo_root, case.reference_photo)
        parts.append(part if part is not None else _text_reference(case.reference_photo))
    for view in case.rendered_views:
        part = _image_part(repo_root, view)
        parts.append(part if part is not None else _text_reference(view))
    return system, [{"role": "user", "content": parts}]


async def _call_design(
    *,
    model_id: str,
    request_factory: RequestFactory,
    system: str,
    messages: list[dict[str, Any]],
) -> str:
    """One OpenAI-compatible chat completion; the raw message content.

    The key stays inside the injected ``request_factory`` (its
    ``Authorization`` header) — this function only assembles the request
    body and extracts the content. A non-dict response body, a missing
    ``choices`` list, or a non-dict message raises :class:`ValueError`
    (the caller maps that to a ``container_error`` outcome; an
    unparseable LLM response is not a design defect).
    """
    response = await request_factory(
        {"model": model_id, "messages": [{"role": "system", "content": system}, *messages]}
    )
    try:
        data = response.json()
    except AttributeError as e:
        raise ValueError("LLM response has no .json()") from e
    if not isinstance(data, dict):
        raise TypeError("LLM response body is not a JSON object")
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ValueError("LLM response has no choices")
    message = choices[0].get("message") if isinstance(choices[0], dict) else None
    if not isinstance(message, dict):
        raise TypeError("LLM response message is not an object")
    content = message.get("content")
    return content if isinstance(content, str) else ""


# ---------------------------------------------------------------------------
# Per-case gate phase
# ---------------------------------------------------------------------------


def _dims_dict(case: GoldenCase) -> dict[str, float] | None:
    if case.expected_dims is None:
        return None
    return {"x": case.expected_dims.x, "y": case.expected_dims.y, "z": case.expected_dims.z}


def run_case_gates(
    *,
    case: GoldenCase,
    repo_root: Path,
    render_result: Any,
    mesh: Any,
    stl_path: str | None,
    pre_mesh: Any = None,
    load_fn: Callable[[str], Any] | None = None,
) -> dict[str, GateResult]:
    """Run the case's declared gates in the pinned 1->7 order.

    A case's ``gate_expectations`` declare the subset of the seven gates
    it must clear (an adversarial refusal case may declare only
    ``compile``); undeclared gates are not run. The runner short-circuits
    on the first failure — the report carries only the gates reached.

    Gate 7 needs the pre-edit and post-edit meshes. ``mesh`` is the
    case's post-edit mesh (already loaded); ``pre_mesh`` is the baseline
    (the caller renders the case's ``baseline_scad`` and injects it).
    When either is missing, gate 7 reports its pinned N/A string via
    :func:`d33d.evals.region_gate.run_region_gate` — neither a pass nor
    a hard fail (the 2D->3D convention is not yet wired).
    """
    gates: dict[str, GateResult] = {}

    if "compile" not in case.gate_expectations:
        return gates

    g1 = _gates.gate1_compiles(
        str(getattr(render_result, "error_class", "")),
        str(getattr(render_result, "stderr", "") or ""),
        str(getattr(render_result, "scad_source", "") or ""),
    )
    gates["compile"] = g1
    if g1.status == "fail":
        return gates

    if "stl_export" in case.gate_expectations:
        g2 = _gates.gate2_stl_export(stl_path)
        gates["stl_export"] = g2
        if g2.status == "fail":
            return gates

    if "watertight_winding" in case.gate_expectations:
        if mesh is None:
            gates["watertight_winding"] = GateResult(
                gate="watertight_winding",
                status="fail",
                failure_class="artifact_error",
                detail="no mesh available for validation",
            )
            return gates
        g3 = _gates.gate3_watertight(mesh)
        gates["watertight_winding"] = GateResult(
            gate="watertight_winding",
            status=g3.status,
            failure_class=g3.failure_class,
            detail=g3.detail,
        )
        if g3.status == "fail":
            return gates

    if "bbox_dims" in case.gate_expectations:
        g4 = _gates.gate4_bbox(mesh, _dims_dict(case))
        gates["bbox_dims"] = GateResult(
            gate="bbox_dims",
            status=g4.status,
            failure_class=g4.failure_class,
            detail=g4.detail,
        )
        if g4.status == "fail":
            return gates

    if "volume_faces" in case.gate_expectations:
        g5 = _gates.gate5_volume(mesh)
        gates["volume_faces"] = GateResult(
            gate="volume_faces",
            status=g5.status,
            failure_class=g5.failure_class,
            detail=g5.detail,
        )
        if g5.status == "fail":
            return gates

    if "slice_dry_run" in case.gate_expectations:
        stl = stl_path if stl_path else ""
        result = run_slice_gate(stl)
        gates["slice_dry_run"] = GateResult(
            gate="slice_dry_run",
            status=result.status,
            failure_class=None if result.status != "fail" else "artifact_error",
            detail=result.detail,
        )
        if result.status == "fail":
            return gates

    if "region_containment" in case.gate_expectations:
        post_mesh = mesh
        if pre_mesh is None:
            # The baseline mesh cannot be resolved from case data alone
            # (the baseline .scad needs a render) — the gate reports its
            # pinned N/A string (not a pass, not a hard fail).
            region = run_region_gate(
                pre_mesh,  # type: ignore[arg-type]
                mesh,  # type: ignore[arg-type]
                None,
                None,
            )
        else:
            region = run_region_gate(pre_mesh, post_mesh, None, None)
        gates["region_containment"] = GateResult(
            gate="region_containment",
            status=region.status,
            failure_class=None,
            detail=region.reason,
        )
        if region.status == "fail":
            return gates

    return gates


# ---------------------------------------------------------------------------
# Per-case run — gates first, judge only when the gates let it through
# ---------------------------------------------------------------------------


async def run_case(
    *,
    case: GoldenCase,
    repo_root: Path,
    render_result: Any,
    mesh: Any = None,
    stl_path: str | None = None,
    model_id: str,
    request_factory: RequestFactory,
    judge_fn: Callable[..., Awaitable[JudgeVerdict]] | None = None,
    pre_mesh: Any = None,
) -> CaseOutcome:
    """Run one golden-set case end to end (gates -> design call -> judge).

    ``render_result`` is the render-worker result for the case's output
    (``RenderResult``-shaped: ``error_class``, ``stderr``, ``stl``).
    ``mesh`` is the trimesh load of that STL (``None`` when absent).
    ``model_id`` is the resolved design-role model (configured via the
    catalogue — never hardcoded here). ``request_factory`` is the
    injected LLM request path (the key stays inside it).

    The gate phase runs first and short-circuits on the first failure —
    a case that fails a gate never reaches the design call or the judge
    (the spec's ordering, enforced structurally). A gate-passing
    candidate then gets a design call and a judge verdict.
    """
    if case.gate_expectations:
        gate_map = run_case_gates(
            case=case,
            repo_root=repo_root,
            render_result=render_result,
            mesh=mesh,
            stl_path=stl_path,
            pre_mesh=pre_mesh,
        )
    else:
        gate_map = {}

    failed = [g for g in gate_map.values() if g.status == "fail"]
    if failed:
        first = failed[0]
        return CaseOutcome(
            case_id=case.case_id,
            kind=case.kind,
            prompt_version=case.prompt.prompt_version,
            prompt_sha256=case.prompt.sha256,
            request=case.request,
            gates=gate_map,
            failure_class=first.failure_class,
            detail=f"{first.gate}: {first.detail}",
        )

    system, messages = case_messages(case, repo_root)
    try:
        scad_source = await _call_design(
            model_id=model_id, request_factory=request_factory, system=system, messages=messages
        )
    except ValueError as e:
        return CaseOutcome(
            case_id=case.case_id,
            kind=case.kind,
            prompt_version=case.prompt.prompt_version,
            prompt_sha256=case.prompt.sha256,
            request=case.request,
            gates=gate_map,
            failure_class="container_error",
            detail=f"design call failed: {e}",
        )

    fn = judge_fn if judge_fn is not None else judge_render
    verdict = await fn(
        JudgeInput(
            case=case,
            repo_root=repo_root,
            scad_source=scad_source,
            stl_path=stl_path,
            model_id=model_id,
            request_factory=request_factory,
        )
    )

    failure_class: str | None = None
    if not verdict.passed and verdict.failure_class is not None:
        if verdict.failure_class in _gates.EVAL_FAILURE_CLASSES:
            failure_class = verdict.failure_class  # type: ignore[assignment]
        else:
            failure_class = "unclassified_syntax_error"

    return CaseOutcome(
        case_id=case.case_id,
        kind=case.kind,
        prompt_version=case.prompt.prompt_version,
        prompt_sha256=case.prompt.sha256,
        request=case.request,
        scad_source=scad_source,
        gates=gate_map,
        judge=verdict,
        failure_class=failure_class,
        detail=verdict.reason,
    )
