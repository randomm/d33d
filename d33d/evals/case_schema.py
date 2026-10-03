"""Per-case schema for the golden set (issue #9, workstream task-golden-set).

The golden set is git-tracked under ``evals/cases/`` as one JSON file per
case. This module is the single source of truth for:

* the **case schema** (:class:`GoldenCase`) — the fields every case file
  must carry so the harness can score it unattended,
* the **seed mix** — the 23-case on-disk composition (the 20-case seed:
  6 primitives including the Qwen smoke baseline / 5 red-marked region
  edits / 3 boolean-topology / 3 photo recreations / 3 adversarial, plus
  the 3 imported-part cases from issue #340),
* **prompt hash-pinning** — every case references its prompt file by
  SHA-256 of the file content, so a prompt edit without a re-pin fails
  the suite and a regression reads as "prompt v7 fails case 12 which
  v5 passed".

Case-file fields, and where each comes from:

The on-disk set is the 23-case composition :data:`SEED_MIX` pins: 6
primitives (5 seed primitives + the Qwen smoke baseline, marked
``is_baseline`` — a reference, not a gate), 5 red-marked region edits,
3 boolean-topology, 3 photo recreations, 3 adversarial (6 + 5 + 3 + 3 +
3 = 20-case seed), plus the 3 imported-part cases issue #340 added
(6 + 5 + 3 + 3 + 3 + 3 = 23). Every case's ``kind`` matches the
composition; the baseline is additionally flagged ``is_baseline`` so the
report shows it as the reference. :data:`SEED_MIX` is this full on-disk
composition, so ``sum(SEED_MIX.values()) == 23`` is directly testable.

* ``case_id`` / ``kind`` — stable identity across prompt versions and the
  seed-mix classifier.
* ``prompt`` — the hash-pinned prompt the case is scored with (see
  :class:`PromptPin` and :func:`prompt_file_hash`). The pin is *content*
  (SHA-256 of the prompt file), not a mutable path.
* ``request`` — the operator's text request, as it would enter the
  design loop.
* ``reference_photo`` — the photo identity (relative path under
  ``evals/cases/``), or None for text-only cases (primitives, some
  adversarial). The Qwen baseline is the one case whose *input* is a
  photo plus rendered views.
* ``expected_dims`` — the stated dimensions in mm, ``{x, y, z}``. This is
  the per-case source of truth for gate 4 (bbox within
  ``max(1%, 0.5mm)`` per axis), independent of any versioned project —
  the ticket's gap-gate finding named this field as the gate's source.
  None for cases that refuse or apply a clearance (no geometry to
  measure).
* ``gate_expectations`` — which of the seven deterministic gates the case
  must pass. Cases that legitimately exercise a later gate's failure
  path (adversarial, some boolean/topology CGAL cases) declare the
  subset they are expected to clear; the harness asserts exactly that
  subset, not "all gates pass".
* ``kind``-specific fields:

  - ``red_region_edit`` — ``edit_op`` (add / remove / move), the
    ``baseline_scad`` (pre-edit model, required so the "changed triangle
    area" denominator of gate 7 is computable from case data alone) and
    the ``selection_polygon`` (per-view polygon(s); the 2D→3D volume
    convention itself is owned by #7, this field only names the 2D
    input it consumes).
  - ``adversarial`` — the ``expected_outcome`` (``graceful_refusal`` or
    ``clearance_applied`` — the two outcome classes adversarial cases are
    scored against, never as compile failures) and the ``diagnostic``
    the agent must produce.

The ``qwen_smoke_baseline`` case is a baseline, not a gate: ``is_baseline``
marks it so the report shows it as the reference every future model is
measured against (the operator confirmed the model works in practice —
the baseline documents that, it does not block).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from d33d.evals.fixtures import check_fixture_containment

#: The seven deterministic gates, in the fixed ticket order. Gates 1–7
#: run before any judge; the judge is stage 8 (the last stage, not a
#: gate) and is out of scope for the golden-set schema — it is a
#: holdout-only concern of the harness.
GATE_NAMES: tuple[str, ...] = (
    "compile",
    "stl_export",
    "watertight_winding",
    "bbox_dims",
    "volume_faces",
    "slice_dry_run",
    "region_containment",
)

#: Pre-check gates, keyed by the case kind that triggers them (issue
#: #340): ``import_guard`` runs inside the gate phase of every
#: ``imported_part`` case (the candidate must import the seeded part at
#: the settled scale and never resize it) BEFORE the declared
#: ``gate_expectations`` run, but it is NOT declared in
#: ``gate_expectations`` — it is implied by the kind.
#:
#: Ordering lives in :func:`d33d.evals.report.case_gate_order` (the report
#: orders a kind's pre-check gate first); this constant only maps kind →
#: pre-check gate name.
KIND_PRECHECK_GATES: dict[str, str] = {"imported_part": "import_guard"}

#: Gate 6/7 N/A markers: a gate reports N/A (neither pass nor hard fail)
#: when its delegate is absent — gate 6 when no headless slicer is
#: invocable, gate 7 when #7's 2D→3D convention has not landed.
GATE_NA_MARKERS: dict[str, str] = {
    # Both markers match the gate modules' NA_REASON strings exactly
    # (d33d.evals.slice_gate.NA_REASON / d33d.evals.region_gate.NA_REASON).
    "slice_dry_run": "N/A — slicer not available headless",
    "region_containment": "N/A, containment convention not available",
}

#: The on-disk golden-set composition, per kind: the 20-case seed (6
#: primitives — 5 seed primitives plus the ``is_baseline`` Qwen smoke
#: baseline within that count — / 5 red-region edits / 3 boolean /
#: 3 photo / 3 adversarial) plus the 3 imported-part cases from issue
#: #340. A valid set must have exactly this count per kind, so it sums
#: to exactly 23 (testable via ``verify_seed``).
SEED_MIX: dict[str, int] = {
    "primitive": 6,
    "red_region_edit": 5,
    "boolean_topology": 3,
    "photo_recreation": 3,
    "adversarial": 3,
    "imported_part": 3,
}

#: Case kinds that carry no expected geometry (they refuse or apply a
#: clearance, so gate 4 has nothing to measure).
_KINDS_WITHOUT_DIMS: frozenset[str] = frozenset({"adversarial"})

#: The two outcome classes adversarial cases are scored against (never
#: as compile failures). The superset = the 10 OpenSCAD classes from
#: #5 + these two.
ADVERSARIAL_OUTCOMES: tuple[str, ...] = ("graceful_refusal", "clearance_applied")

#: Print tolerance for the 0.4 mm nozzle: features below this are below
#: print tolerance and must be refused or cleared (the adversarial
#: "detail below print tolerance" class).
PRINT_TOLERANCE_MM = 1.0

#: The Qwen smoke baseline case id. One reference photo + three rendered
#: views → OpenSCAD, scored on compile, watertightness, parameter
#: fidelity (gate 4 bbox) and slice success. Marked baseline, not gate.
QWEN_SMOKE_CASE_ID = "qwen-smoke-baseline"


class PartRef(BaseModel):
    """The imported-part mesh a case renders against (issue #340).

    ``fixture`` is a path under ``evals/cases/fixtures/`` (relative to the
    repo root); ``scale`` is the settled file→mm factor the import guard
    accepts for this case (1.0 for an mm part — the guard compares
    numerically within ``SCALE_FACTOR_TOL``, never by string).
    """

    fixture: str = Field(min_length=1)
    scale: float = Field(gt=0)


class DimsMm(BaseModel):
    """Stated dimensions in millimetres — gate 4's per-case source of truth."""

    x: float = Field(gt=0)
    y: float = Field(gt=0)
    z: float = Field(gt=0)


class PromptPin(BaseModel):
    """A hash-pinned reference to a prompt file.

    ``prompt_version`` is the human-facing version tag (v1, v2, ...);
    ``sha256`` is the content hash that actually pins it. The two must
    agree with the file on disk or the test suite fails — see
    :func:`check_prompt_pin`.
    """

    prompt_version: str = Field(min_length=1, pattern=r"^v[0-9]+$")
    path: str = Field(min_length=1)
    sha256: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")


class SelectionPolygon(BaseModel):
    """Per-view selection polygon(s) for a red-region edit case.

    One polygon per view the lasso is drawn in; each is a list of
    ``[x, y]`` points in the view's image space. The 2D→3D bounding
    volume convention (#7) lifts these; the case file only names the 2D
    input so the gate-7 denominator is computable from case data alone.
    """

    view: str = Field(min_length=1)
    points: list[list[float]] = Field(min_length=3)


class RedRegionEdit(BaseModel):
    """Fields for a ``red_region_edit`` case: add / remove / move."""

    edit_op: Literal["add", "remove", "move"]
    baseline_scad: str = Field(min_length=1)
    selection_polygons: list[SelectionPolygon] = Field(min_length=1)


class AdversarialSpec(BaseModel):
    """Fields for an ``adversarial`` case.

    The case must be handled gracefully — a diagnostic refusal or an
    applied clearance — never a silently broken model. Adversarial cases
    are scored against :data:`ADVERSARIAL_OUTCOMES`, never as compile
    failures.
    """

    expected_outcome: Literal["graceful_refusal", "clearance_applied"]
    diagnostic: str = Field(min_length=1)


class GoldenCase(BaseModel):
    """One golden-set case (one JSON file under ``evals/cases/``)."""

    case_id: str = Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9-]*$")
    kind: Literal[
        "primitive",
        "red_region_edit",
        "boolean_topology",
        "photo_recreation",
        "adversarial",
        "imported_part",
    ]
    prompt: PromptPin
    request: str = Field(min_length=1)

    reference_photo: str | None = None
    rendered_views: list[str] = Field(default_factory=list)

    expected_dims: DimsMm | None = None

    gate_expectations: list[str] = Field(min_length=1)

    is_baseline: bool = False
    baseline_note: str | None = None

    red_region_edit: RedRegionEdit | None = None
    adversarial: AdversarialSpec | None = None
    part: PartRef | None = None

    @field_validator("gate_expectations")
    @classmethod
    def _gates_must_be_valid_and_unique(cls, v: list[str]) -> list[str]:
        if len(v) != len(set(v)):
            raise ValueError("duplicate gate names in gate_expectations")
        unknown = set(v) - set(GATE_NAMES)
        if unknown:
            raise ValueError(f"unknown gates in gate_expectations: {sorted(unknown)}")
        return v

    @field_validator("gate_expectations")
    @classmethod
    def _dims_required_when_bbox_expected(cls, v: list[str], info) -> list[str]:
        """Gate 4 (bbox) requires expected_dims — nothing to measure otherwise."""
        data = info.data
        if "bbox_dims" in v:
            dims = data.get("expected_dims")
            if dims is None:
                raise ValueError("gate 'bbox_dims' requires expected_dims")
        return v

    @field_validator("expected_dims")
    @classmethod
    def _dims_forbidden_for_refusing_kinds(cls, v: DimsMm | None, info) -> DimsMm | None:
        kind = info.data.get("kind")
        if v is not None and kind in _KINDS_WITHOUT_DIMS:
            raise ValueError(
                f"kind {kind!r} refuses or applies a clearance — expected_dims must be null"
            )
        return v

    @field_validator("red_region_edit")
    @classmethod
    def _region_edit_only_for_its_kind(
        cls, v: RedRegionEdit | None, info
    ) -> RedRegionEdit | None:
        kind = info.data.get("kind")
        if v is not None and kind != "red_region_edit":
            raise ValueError("red_region_edit field only valid for kind=red_region_edit")
        return v

    @field_validator("adversarial")
    @classmethod
    def _adversarial_only_for_its_kind(cls, v: AdversarialSpec | None, info) -> AdversarialSpec | None:
        kind = info.data.get("kind")
        if v is not None and kind != "adversarial":
            raise ValueError("adversarial field only valid for kind=adversarial")
        return v

    @field_validator("adversarial")
    @classmethod
    def _adversarial_requires_outcome(cls, v: AdversarialSpec | None, info) -> AdversarialSpec | None:
        if v is None:
            return None
        kind = info.data.get("kind")
        if kind == "adversarial" and v is None:
            raise ValueError("kind=adversarial requires the adversarial spec")
        return v

    @field_validator("part")
    @classmethod
    def _part_only_for_imported_kind(
        cls, v: PartRef | None, info
    ) -> PartRef | None:
        kind = info.data.get("kind")
        if v is not None and kind != "imported_part":
            raise ValueError("part field only valid for kind=imported_part")
        return v

    @model_validator(mode="after")
    def _part_required_for_imported_kind(self) -> GoldenCase:
        """An ``imported_part`` case REQUIRES its part ref (the import
        guard is schema-guaranteed to have ``case.part``), and no other
        kind may carry one."""
        if self.kind == "imported_part" and self.part is None:
            raise ValueError("kind=imported_part requires a part ref")
        return self


def prompt_file_hash(repo_root: Path, prompt_path: str) -> str:
    """SHA-256 (hex) of a prompt file's content.

    The pin is over the *content*, not the path: renaming the file keeps
    the pin valid; editing one character breaks it. This is what makes
    "prompt v7 fails case 12 which v5 passed" readable — a regression is
    attributable to a prompt *content* change.
    """
    full = repo_root / prompt_path
    if not full.is_file():
        raise FileNotFoundError(f"prompt file missing: {full}")
    return hashlib.sha256(full.read_bytes()).hexdigest()


def check_prompt_pin(repo_root: Path, pin: PromptPin) -> str:
    """Verify a :class:`PromptPin` against the prompt file on disk.

    Returns the actual content hash; raises :class:`ValueError` (drift:
    content changed, pin didn't) or :class:`FileNotFoundError` (file
    gone) otherwise. The test suite calls this for every case so a
    prompt edit without a re-pin is a hard failure, not a silent
    regression.
    """
    actual = prompt_file_hash(repo_root, pin.path)
    if actual != pin.sha256:
        raise ValueError(
            f"prompt drift: {pin.path} is pinned as {pin.prompt_version} "
            f"({pin.sha256[:12]}…) but content hashes to {actual[:12]}… — "
            f"re-pin with the new hash or restore the file"
        )
    return actual


def load_case_file(path: Path) -> GoldenCase:
    """Parse and validate one case file (JSON → :class:`GoldenCase`)."""
    data = json.loads(path.read_text())
    return GoldenCase.model_validate(data)


def load_golden_set(cases_dir: Path, repo_root: Path | None = None) -> dict[str, GoldenCase]:
    """Load every ``*.json`` under ``cases_dir`` and validate each.

    Returns ``{case_id: GoldenCase}``. Duplicate case ids (by filename
    stem or ``case_id`` field) are a hard error — case identity must be
    stable across prompt versions.
    """
    root = repo_root if repo_root is not None else cases_dir
    cases: dict[str, GoldenCase] = {}
    files = sorted(cases_dir.glob("*.json"))
    for f in files:
        case = load_case_file(f)
        if case.case_id in cases:
            raise ValueError(f"duplicate case_id {case.case_id!r} ({f.name})")
        cases[case.case_id] = case
    for case in cases.values():
        check_prompt_pin(root, case.prompt)
    return cases


def seed_mix_actual(cases: dict[str, GoldenCase]) -> dict[str, int]:
    """Count all cases per kind (baseline included).

    The baseline is the 6th primitive — part of the on-disk composition,
    not an extra case. So the mix is counted over the full set.
    """
    mix: dict[str, int] = {k: 0 for k in SEED_MIX}
    for c in cases.values():
        mix[c.kind] = mix.get(c.kind, 0) + 1
    return mix


def verify_seed(cases: dict[str, GoldenCase], repo_root: Path | None = None) -> list[str]:
    """Check the seed against :data:`SEED_MIX`; return a list of violations.

    Empty list means the seed is valid: the on-disk total is at least 20,
    exactly the pinned count per kind, every red-region edit has a
    baseline + selection polygons (gate 7 denominator), every adversarial
    case scores against an outcome class, every imported-part case pins a
    part fixture that exists on disk, and the Qwen smoke baseline
    exists and is marked as baseline. An empty list is what the test
    asserts.

    The baseline (``is_baseline``) is the 6th primitive within the
    20-case seed — part of the set, not a 21st case. The per-kind mix is
    therefore counted over the full set, and ``SEED_MIX`` sums to 23
    (the 20-case seed plus the 3 imported-part cases of issue #340).
    """
    violations: list[str] = []

    mix = seed_mix_actual(cases)
    if len(cases) < 20:
        violations.append(f"golden set has {len(cases)} cases, floor is 20")
    if len(cases) != sum(SEED_MIX.values()):
        violations.append(
            f"golden set has {len(cases)} cases, SEED_MIX sums to "
            f"{sum(SEED_MIX.values())}"
        )
    for kind, expected in SEED_MIX.items():
        actual = mix.get(kind, 0)
        if actual != expected:
            violations.append(f"kind {kind!r}: expected {expected} cases, got {actual}")

    for cid, c in cases.items():
        if c.kind == "imported_part":
            # The part ref is schema-guaranteed (GoldenCase requires it
            # for this kind) — only the on-disk checks can still fail.
            if repo_root is None:
                violations.append(f"{cid}: cannot verify fixture without repo_root")
                continue
            containment = check_fixture_containment(repo_root, c.part.fixture)
            if containment is not None:
                violations.append(f"{cid}: {containment}")
                continue
            fixture = repo_root / c.part.fixture
            if not fixture.is_file():
                violations.append(f"{cid}: part fixture {c.part.fixture!r} missing on disk")

    for cid, c in cases.items():
        if c.kind == "red_region_edit":
            if c.red_region_edit is None:
                violations.append(f"{cid}: red_region_edit case missing its spec")
            elif not c.red_region_edit.baseline_scad:
                violations.append(f"{cid}: region-edit baseline_scad empty")
            elif not c.red_region_edit.selection_polygons:
                violations.append(f"{cid}: region-edit selection_polygons empty")

        if c.kind == "adversarial":
            if c.adversarial is None:
                violations.append(f"{cid}: adversarial case missing its spec")
            elif c.adversarial.expected_outcome not in ADVERSARIAL_OUTCOMES:
                violations.append(
                    f"{cid}: adversarial outcome {c.adversarial.expected_outcome!r} "
                    f"not in {ADVERSARIAL_OUTCOMES}"
                )

    baseline = cases.get(QWEN_SMOKE_CASE_ID)
    if baseline is None:
        violations.append(f"missing the Qwen smoke baseline case {QWEN_SMOKE_CASE_ID!r}")
    else:
        if not baseline.is_baseline:
            violations.append(f"{QWEN_SMOKE_CASE_ID}: must be marked is_baseline=true")
        if baseline.reference_photo is None:
            violations.append(f"{QWEN_SMOKE_CASE_ID}: requires a reference_photo")
        if len(baseline.rendered_views) < 3:
            violations.append(
                f"{QWEN_SMOKE_CASE_ID}: requires at least 3 rendered views, "
                f"got {len(baseline.rendered_views)}"
            )
        if "slice_dry_run" not in baseline.gate_expectations:
            violations.append(f"{QWEN_SMOKE_CASE_ID}: baseline is scored on slice success")
        for gate in ("compile", "watertight_winding", "bbox_dims"):
            if gate not in baseline.gate_expectations:
                violations.append(f"{QWEN_SMOKE_CASE_ID}: baseline not scored on {gate!r}")

    return violations
