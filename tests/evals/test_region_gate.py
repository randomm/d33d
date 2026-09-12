"""Fast tests for gate 7 — region containment (ticket #8, workstream
task-region).

Scope (per the workstream brief):

- Gate 7 consumes ``d33d.print_validation.check_containment`` (owned by
  ticket #7) and the threshold ``d33d.print_validation.
  containment_threshold_pct`` — this module does NOT re-implement them.
- The N/A fallback: when the 2D→3D lasso-to-volume convention is not yet
  available, the harness must report ``"N/A, containment convention not
  available"`` — not a pass, not a hard fail.

Fixture strategy (no Docker, no real STL files):

- The pre/post mesh pair is built in memory with trimesh primitives.
  The pre mesh is a 10×10×10 mm box at the origin; the region bounding
  volume is the same box's bbox, ``(-5,-5,-5)`` to ``(5,5,5)``.
- Pass case: a 4 mm cube added entirely inside the region → 0 % spillover.
- Fail case: a 4 mm cube straddling the region boundary → 1/6 ≈ 16.67 %
  spillover (deterministic, verified analytically and empirically).

The threshold is always read via the named environment variable
``D33D_CONTAINMENT_THRESHOLD_PCT`` (default 5.0); tests use
``monkeypatch`` to control it explicitly.
"""

from __future__ import annotations

import pytest
import trimesh

from d33d import print_validation as pv
from d33d.evals.region_gate import NA_REASON, run_region_gate

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _box(
    size: tuple[float, float, float], center: tuple[float, float, float]
) -> trimesh.Trimesh:
    mesh = trimesh.creation.box(extents=size)
    mesh.apply_translation(center)
    return mesh


@pytest.fixture
def region_bbox() -> tuple[tuple[float, float, float], tuple[float, float, float]]:
    """The 10×10×10 mm region bounding volume centred at the origin."""
    return ((-5.0, -5.0, -5.0), (5.0, 5.0, 5.0))


@pytest.fixture
def pre_mesh() -> trimesh.Trimesh:
    """The pre-edit baseline mesh: a single 10 mm box at the origin."""
    return _box((10.0, 10.0, 10.0), (0.0, 0.0, 0.0))


@pytest.fixture
def post_mesh_pass(pre_mesh: trimesh.Trimesh) -> trimesh.Trimesh:
    """Post-edit mesh where the added nub is entirely inside the region.

    A 4 mm cube centred at (2, 0, 0) — spans x ∈ [0, 4], well inside
    the region boundary at x = 5.
    """
    nub = _box((4.0, 4.0, 4.0), (2.0, 0.0, 0.0))
    return trimesh.util.concatenate([pre_mesh, nub])


@pytest.fixture
def post_mesh_fail(pre_mesh: trimesh.Trimesh) -> trimesh.Trimesh:
    """Post-edit mesh where the added nub straddles the region boundary.

    A 4 mm cube centred at (3.2, 0, 0) — spans x ∈ [1.2, 5.2]; the +x
    face centroid at x = 5.2 is outside the region, giving a deterministic
    1/6 ≈ 16.67 % spillover (verified analytically and empirically).
    """
    nub = _box((4.0, 4.0, 4.0), (3.2, 0.0, 0.0))
    return trimesh.util.concatenate([pre_mesh, nub])


# ---------------------------------------------------------------------------
# N/A fallback
# ---------------------------------------------------------------------------


def test_na_when_bbox_min_is_none(
    pre_mesh: trimesh.Trimesh,
    post_mesh_pass: trimesh.Trimesh,
) -> None:
    """Gate 7 reports N/A when the caller cannot supply bbox_min
    (the 2D→3D convention is not available)."""
    result = run_region_gate(pre_mesh, post_mesh_pass, None, (5.0, 5.0, 5.0))
    assert result.status == "na"
    assert result.reason == NA_REASON
    assert result.spillover_pct == 0.0
    assert result.threshold_pct == 0.0


def test_na_when_bbox_max_is_none(
    pre_mesh: trimesh.Trimesh,
    post_mesh_pass: trimesh.Trimesh,
) -> None:
    """Gate 7 reports N/A when the caller cannot supply bbox_max."""
    result = run_region_gate(pre_mesh, post_mesh_pass, (-5.0, -5.0, -5.0), None)
    assert result.status == "na"
    assert result.reason == NA_REASON


def test_na_when_both_bbox_are_none(
    pre_mesh: trimesh.Trimesh,
    post_mesh_pass: trimesh.Trimesh,
) -> None:
    """Gate 7 reports N/A when neither bound is available."""
    result = run_region_gate(pre_mesh, post_mesh_pass, None, None)
    assert result.status == "na"
    assert result.reason == NA_REASON
    assert result.reason == "N/A, containment convention not available"


def test_na_reason_is_not_a_pass_or_fail(
    pre_mesh: trimesh.Trimesh,
    post_mesh_pass: trimesh.Trimesh,
) -> None:
    """The N/A status is distinct from both pass and fail — the report
    must not collapse it into either bucket."""
    result = run_region_gate(pre_mesh, post_mesh_pass, None, None)
    assert result.status not in ("pass", "fail")


# ---------------------------------------------------------------------------
# Pass path — edit stays inside the region
# ---------------------------------------------------------------------------


def test_pass_when_edit_fully_inside_region(
    pre_mesh: trimesh.Trimesh,
    post_mesh_pass: trimesh.Trimesh,
    region_bbox: tuple[tuple[float, float, float], tuple[float, float, float]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fully-contained edit passes the gate (0 % spillover ≤ 5 %
    baseline)."""
    monkeypatch.delenv(pv.CONTAINMENT_THRESHOLD_ENV_VAR, raising=False)
    bbox_min, bbox_max = region_bbox
    result = run_region_gate(pre_mesh, post_mesh_pass, bbox_min, bbox_max)
    assert result.status == "pass"
    assert result.spillover_pct == pytest.approx(0.0, abs=1e-9)
    assert result.threshold_pct == pytest.approx(5.0)


def test_pass_when_no_changes(
    pre_mesh: trimesh.Trimesh,
    region_bbox: tuple[tuple[float, float, float], tuple[float, float, float]],
) -> None:
    """An edit that changed nothing trivially passes (0 % spillover)."""
    bbox_min, bbox_max = region_bbox
    result = run_region_gate(pre_mesh, pre_mesh, bbox_min, bbox_max)
    assert result.status == "pass"
    assert result.spillover_pct == pytest.approx(0.0, abs=1e-9)


# ---------------------------------------------------------------------------
# Fail path — edit spills outside the region
# ---------------------------------------------------------------------------


def test_fail_when_edit_spills_outside_region(
    pre_mesh: trimesh.Trimesh,
    post_mesh_fail: trimesh.Trimesh,
    region_bbox: tuple[tuple[float, float, float], tuple[float, float, float]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An edit whose spillover exceeds the 5 % baseline threshold is
    flagged as a fail."""
    monkeypatch.delenv(pv.CONTAINMENT_THRESHOLD_ENV_VAR, raising=False)
    bbox_min, bbox_max = region_bbox
    result = run_region_gate(pre_mesh, post_mesh_fail, bbox_min, bbox_max)
    assert result.status == "fail"
    assert result.spillover_pct > 5.0
    assert result.threshold_pct == pytest.approx(5.0)


# ---------------------------------------------------------------------------
# Threshold is consumed, not re-implemented
# ---------------------------------------------------------------------------


def test_threshold_read_from_named_env_var(
    pre_mesh: trimesh.Trimesh,
    post_mesh_fail: trimesh.Trimesh,
    region_bbox: tuple[tuple[float, float, float], tuple[float, float, float]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The gate reads the threshold from the named environment variable
    (``D33D_CONTAINMENT_THRESHOLD_PCT``) — it does not hardcode 5.0.
    Setting a very high threshold flips a fail to a pass."""
    bbox_min, bbox_max = region_bbox

    # High threshold: the same straddling edit now passes.
    monkeypatch.setenv(pv.CONTAINMENT_THRESHOLD_ENV_VAR, "100.0")
    lenient = run_region_gate(pre_mesh, post_mesh_fail, bbox_min, bbox_max)
    assert lenient.status == "pass"
    assert lenient.threshold_pct == pytest.approx(100.0)

    # Low threshold: the same edit fails even harder.
    monkeypatch.setenv(pv.CONTAINMENT_THRESHOLD_ENV_VAR, "1.0")
    strict = run_region_gate(pre_mesh, post_mesh_fail, bbox_min, bbox_max)
    assert strict.status == "fail"
    assert strict.threshold_pct == pytest.approx(1.0)


def test_invalid_threshold_env_var_falls_back_to_default(
    pre_mesh: trimesh.Trimesh,
    post_mesh_fail: trimesh.Trimesh,
    region_bbox: tuple[tuple[float, float, float], tuple[float, float, float]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A malformed env value falls back to the documented 5 % default
    (fail-closed) rather than crashing the gate."""
    monkeypatch.setenv(pv.CONTAINMENT_THRESHOLD_ENV_VAR, "not-a-number")
    bbox_min, bbox_max = region_bbox
    result = run_region_gate(pre_mesh, post_mesh_fail, bbox_min, bbox_max)
    assert result.threshold_pct == pytest.approx(pv.DEFAULT_CONTAINMENT_THRESHOLD_PCT)
    assert result.status == "fail"  # 16.67 % > 5 % default


# ---------------------------------------------------------------------------
# Result type shape
# ---------------------------------------------------------------------------


def test_result_type_is_frozen_dataclass(
    pre_mesh: trimesh.Trimesh,
    post_mesh_pass: trimesh.Trimesh,
    region_bbox: tuple[tuple[float, float, float], tuple[float, float, float]],
) -> None:
    """RegionGateResult is a frozen dataclass — field assignment raises.

    ``dataclasses.replace`` is the sanctioned way to get a modified copy
    (it never mutates the original); a direct assignment to a declared
    field on the original is the freeze contract and raises
    ``AttributeError``. The assignment is exercised through a small typed
    helper so the frozen dataclass's ``__setattr__`` is still triggered
    without a type suppression (the helper keeps the assignment from
    reading as a typo).
    """
    import dataclasses

    def _assign_status(r, value: str) -> None:
        # A string-keyed assignment through a helper (the frozen dataclass
        # raises AttributeError on any declared-field assignment, and the
        # helper keeps the assignment from reading as a typo).
        r.status = value

    bbox_min, bbox_max = region_bbox
    result = run_region_gate(pre_mesh, post_mesh_pass, bbox_min, bbox_max)
    original_status = result.status
    replaced = dataclasses.replace(result, status="fail")
    assert replaced.status == "fail"
    assert result.status == original_status  # the original is untouched
    with pytest.raises(AttributeError):
        _assign_status(result, "fail")
    assert result.status == original_status  # still untouched after the raise


def test_result_exposes_all_three_fields(
    pre_mesh: trimesh.Trimesh,
    post_mesh_pass: trimesh.Trimesh,
    region_bbox: tuple[tuple[float, float, float], tuple[float, float, float]],
) -> None:
    """The result exposes status, spillover_pct, threshold_pct, and
    reason — all four fields the report needs."""
    bbox_min, bbox_max = region_bbox
    result = run_region_gate(pre_mesh, post_mesh_pass, bbox_min, bbox_max)
    assert hasattr(result, "status")
    assert hasattr(result, "spillover_pct")
    assert hasattr(result, "threshold_pct")
    assert hasattr(result, "reason")
    assert isinstance(result.reason, str)
    assert len(result.reason) > 0


# ---------------------------------------------------------------------------
# Reason string contains both measured and threshold values
# ---------------------------------------------------------------------------


def test_reason_contains_spillover_and_threshold(
    pre_mesh: trimesh.Trimesh,
    post_mesh_fail: trimesh.Trimesh,
    region_bbox: tuple[tuple[float, float, float], tuple[float, float, float]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The reason string for a pass/fail verdict includes both the
    measured spillover and the threshold so the report is self-
    explanatory."""
    monkeypatch.delenv(pv.CONTAINMENT_THRESHOLD_ENV_VAR, raising=False)
    bbox_min, bbox_max = region_bbox
    result = run_region_gate(pre_mesh, post_mesh_fail, bbox_min, bbox_max)
    assert f"{result.spillover_pct:.4f}" in result.reason
    assert f"{result.threshold_pct:.4f}" in result.reason


def test_reason_is_exactly_na_string_for_na_status(
    pre_mesh: trimesh.Trimesh,
    post_mesh_pass: trimesh.Trimesh,
) -> None:
    """The N/A reason is exactly the spec-pinned string, so the report
    can match it verbatim."""
    result = run_region_gate(pre_mesh, post_mesh_pass, None, None)
    assert result.reason == "N/A, containment convention not available"
