"""Fast-layer tests for the region-selection containment gate (ticket #6).

Scope of this test file (per the ticket's task-d split): the CONTAINMENT
METRIC only — the fraction of changed triangle area falling outside the
selected region's 3D bounding volume — independent of how that volume was
resolved (module registry / lasso-to-3D lift is other workstreams' concern).

Pinned by the ticket:
    - the metric is "fraction of changed triangle area outside the
      selected region's 3D bounding volume", flagged when it exceeds a
      threshold (baseline 5%)
    - the threshold MUST be a named configurable value (env), never a
      hardcoded code constant, and the measured fraction is logged per run
"""

from __future__ import annotations

import logging

import numpy as np
import pytest
import trimesh

from d33d import print_validation as pv

# ---------------------------------------------------------------------------
# Fixtures: pre/post meshes with known spillover
# ---------------------------------------------------------------------------


def _box(
    size: tuple[float, float, float], center: tuple[float, float, float]
) -> trimesh.Trimesh:
    mesh = trimesh.creation.box(extents=size)
    mesh.apply_translation(center)
    return mesh


@pytest.fixture
def region_bbox() -> tuple[tuple[float, float, float], tuple[float, float, float]]:
    """A 10x10x10mm region volume centred at the origin."""
    return ((-5.0, -5.0, -5.0), (5.0, 5.0, 5.0))


@pytest.fixture
def base_mesh() -> trimesh.Trimesh:
    """The pre-edit mesh: a single 10mm box fully inside the region."""
    return _box((10.0, 10.0, 10.0), (0.0, 0.0, 0.0))


@pytest.fixture
def edited_mesh_contained(base_mesh: trimesh.Trimesh) -> trimesh.Trimesh:
    """Post-edit mesh: an added 2mm nub, entirely inside the region bbox."""
    nub = _box((2.0, 2.0, 2.0), (3.0, 3.0, 3.0))
    return trimesh.util.concatenate([base_mesh, nub])


@pytest.fixture
def edited_mesh_spillover(base_mesh: trimesh.Trimesh) -> trimesh.Trimesh:
    """Post-edit mesh: an added nub straddling the region bbox boundary,
    so only part of its triangle area is outside the region."""
    nub = _box((4.0, 4.0, 4.0), (6.0, 0.0, 0.0))
    return trimesh.util.concatenate([base_mesh, nub])


# ---------------------------------------------------------------------------
# changed_faces
# ---------------------------------------------------------------------------


def test_changed_faces_empty_when_meshes_identical(base_mesh: trimesh.Trimesh) -> None:
    """No faces changed when pre and post are the same mesh."""
    changed = pv.changed_faces(base_mesh, base_mesh)
    assert len(changed) == 0


def test_changed_faces_detects_added_geometry(
    base_mesh: trimesh.Trimesh, edited_mesh_contained: trimesh.Trimesh
) -> None:
    """Faces belonging to the added nub are reported as changed; faces
    shared with the pre-edit mesh are not."""
    changed = pv.changed_faces(base_mesh, edited_mesh_contained)
    assert len(changed) > 0
    # The nub added 12 triangles (2 per box face x 6 faces); the changed
    # set must not include all of edited_mesh_contained's faces (i.e. the
    # untouched box body must be excluded).
    assert len(changed) < len(edited_mesh_contained.faces)


# ---------------------------------------------------------------------------
# spillover_fraction
# ---------------------------------------------------------------------------


def test_spillover_fraction_zero_when_fully_contained(
    base_mesh: trimesh.Trimesh,
    edited_mesh_contained: trimesh.Trimesh,
    region_bbox: tuple[tuple[float, float, float], tuple[float, float, float]],
) -> None:
    """The added nub is entirely inside the region bbox -> 0% spillover."""
    changed = pv.changed_faces(base_mesh, edited_mesh_contained)
    bbox_min, bbox_max = region_bbox
    fraction = pv.spillover_fraction(edited_mesh_contained, changed, bbox_min, bbox_max)
    assert fraction == pytest.approx(0.0, abs=1e-9)


def test_spillover_fraction_positive_when_partially_outside(
    base_mesh: trimesh.Trimesh,
    edited_mesh_spillover: trimesh.Trimesh,
    region_bbox: tuple[tuple[float, float, float], tuple[float, float, float]],
) -> None:
    """The added nub pokes outside the region bbox -> nonzero spillover,
    and the fraction is bounded in [0, 1]."""
    changed = pv.changed_faces(base_mesh, edited_mesh_spillover)
    bbox_min, bbox_max = region_bbox
    fraction = pv.spillover_fraction(edited_mesh_spillover, changed, bbox_min, bbox_max)
    assert fraction > 0.0
    assert fraction <= 1.0


def test_spillover_fraction_full_when_all_outside(
    base_mesh: trimesh.Trimesh,
    region_bbox: tuple[tuple[float, float, float], tuple[float, float, float]],
) -> None:
    """A changed nub entirely outside the region bbox -> ~100% spillover."""
    nub = _box((2.0, 2.0, 2.0), (50.0, 50.0, 50.0))
    edited = trimesh.util.concatenate([base_mesh, nub])
    changed = pv.changed_faces(base_mesh, edited)
    bbox_min, bbox_max = region_bbox
    fraction = pv.spillover_fraction(edited, changed, bbox_min, bbox_max)
    assert fraction == pytest.approx(1.0, abs=1e-9)


def test_spillover_fraction_zero_when_no_changed_faces(
    base_mesh: trimesh.Trimesh,
    region_bbox: tuple[tuple[float, float, float], tuple[float, float, float]],
) -> None:
    """No changed faces at all -> 0% spillover (nothing to be outside)."""
    bbox_min, bbox_max = region_bbox
    fraction = pv.spillover_fraction(
        base_mesh, np.array([], dtype=np.int64), bbox_min, bbox_max
    )
    assert fraction == pytest.approx(0.0, abs=1e-9)


# ---------------------------------------------------------------------------
# threshold: named configurable, never a hardcoded constant
# ---------------------------------------------------------------------------


def test_default_threshold_is_five_percent(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no env override, the threshold defaults to the spec's initial
    5% guess."""
    monkeypatch.delenv(pv.CONTAINMENT_THRESHOLD_ENV_VAR, raising=False)
    assert pv.containment_threshold_pct() == pytest.approx(5.0)


def test_threshold_reads_from_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    """The threshold is a NAMED CONFIGURABLE value read from the
    environment, not a code constant, so it can be tuned once the golden
    set produces real data."""
    monkeypatch.setenv(pv.CONTAINMENT_THRESHOLD_ENV_VAR, "12.5")
    assert pv.containment_threshold_pct() == pytest.approx(12.5)


def test_invalid_threshold_env_var_falls_back_to_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A malformed env value fails closed to the documented default rather
    than crashing the gate."""
    monkeypatch.setenv(pv.CONTAINMENT_THRESHOLD_ENV_VAR, "not-a-number")
    assert pv.containment_threshold_pct() == pytest.approx(
        pv.DEFAULT_CONTAINMENT_THRESHOLD_PCT
    )


# ---------------------------------------------------------------------------
# check_containment: the gate entry point, both sides of the boundary
# ---------------------------------------------------------------------------


def test_check_containment_passes_within_threshold(
    base_mesh: trimesh.Trimesh,
    edited_mesh_contained: trimesh.Trimesh,
    region_bbox: tuple[tuple[float, float, float], tuple[float, float, float]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fully-contained edit passes the gate (0% <= 5% baseline)."""
    monkeypatch.delenv(pv.CONTAINMENT_THRESHOLD_ENV_VAR, raising=False)
    bbox_min, bbox_max = region_bbox
    result = pv.check_containment(base_mesh, edited_mesh_contained, bbox_min, bbox_max)
    assert result.ok is True
    assert result.spillover_pct == pytest.approx(0.0, abs=1e-9)
    assert result.threshold_pct == pytest.approx(5.0)


def test_check_containment_fails_above_threshold(
    base_mesh: trimesh.Trimesh,
    region_bbox: tuple[tuple[float, float, float], tuple[float, float, float]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An edit whose spillover exceeds the threshold is flagged."""
    monkeypatch.delenv(pv.CONTAINMENT_THRESHOLD_ENV_VAR, raising=False)
    nub = _box((2.0, 2.0, 2.0), (50.0, 50.0, 50.0))
    edited = trimesh.util.concatenate([base_mesh, nub])
    bbox_min, bbox_max = region_bbox
    result = pv.check_containment(base_mesh, edited, bbox_min, bbox_max)
    assert result.ok is False
    assert result.spillover_pct > result.threshold_pct


def test_check_containment_respects_custom_threshold(
    base_mesh: trimesh.Trimesh,
    edited_mesh_spillover: trimesh.Trimesh,
    region_bbox: tuple[tuple[float, float, float], tuple[float, float, float]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Swapping the env value changes the pass/fail outcome for the same
    edit, proving the threshold is not baked in as a code constant."""
    bbox_min, bbox_max = region_bbox

    monkeypatch.setenv(pv.CONTAINMENT_THRESHOLD_ENV_VAR, "0.01")
    strict = pv.check_containment(base_mesh, edited_mesh_spillover, bbox_min, bbox_max)
    assert strict.ok is False

    monkeypatch.setenv(pv.CONTAINMENT_THRESHOLD_ENV_VAR, "100.0")
    lenient = pv.check_containment(base_mesh, edited_mesh_spillover, bbox_min, bbox_max)
    assert lenient.ok is True


def test_check_containment_logs_measured_fraction_per_run(
    base_mesh: trimesh.Trimesh,
    edited_mesh_contained: trimesh.Trimesh,
    region_bbox: tuple[tuple[float, float, float], tuple[float, float, float]],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Every run logs the measured spillover fraction (spec: logged per
    run, not silently dropped after the pass/fail decision)."""
    monkeypatch.delenv(pv.CONTAINMENT_THRESHOLD_ENV_VAR, raising=False)
    bbox_min, bbox_max = region_bbox
    with caplog.at_level(logging.INFO, logger=pv.logger.name):
        pv.check_containment(base_mesh, edited_mesh_contained, bbox_min, bbox_max)
    assert any("spillover" in record.message.lower() for record in caplog.records)


def test_check_containment_no_changes_is_trivially_contained(
    base_mesh: trimesh.Trimesh,
    region_bbox: tuple[tuple[float, float, float], tuple[float, float, float]],
) -> None:
    """An edit that changed nothing has 0% spillover and passes."""
    bbox_min, bbox_max = region_bbox
    result = pv.check_containment(base_mesh, base_mesh, bbox_min, bbox_max)
    assert result.ok is True
    assert result.spillover_pct == pytest.approx(0.0, abs=1e-9)
