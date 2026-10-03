"""Pure-function unit-settlement coverage (issue #340, workstream task-unit).

STL carries no units; the settlement branches (``d33d/part_units``) decide
what the design loop may assume:

- plausible STL (>= 5 mm, fits the QIDI envelope read as mm) → assumed mm;
- implausible STL (< 5 mm or over the envelope) → unsettled, with the
  candidate unit options listed likeliest first (inch > cm > mm within each
  tier);
- 3MF is never asked: an ABSENT unit (``None``) resolves to scale 1.0.

This is the deterministic stand-in for a unit-settlement eval: no model, no
Docker, no fixture file — the same fixtures the import cases reference
(``evals/cases/fixtures/part.stl``, derived from ``tests/fixtures/stl/``)
classify identically, so the pure functions on their known extents ARE the
case's settlement behavior.
"""

from __future__ import annotations

import pytest

from d33d.part_units import classify_stl_units, mm_factor_for_unit
from d33d.part_mesh import PartUploadError

# Known extents of the git-tracked fixtures (verified via trimesh):
# tests/fixtures/stl/box_20mm.stl → (20, 20, 20); holey.stl → (20, 20, 20);
# tests/fixtures/stl/over_envelope.stl → (400, 20, 20). The imported-part
# eval fixture evals/cases/fixtures/part.stl is one of these, so its mm
# reading is plausible by construction.
BOX_20MM_EXTENTS = (20.0, 20.0, 20.0)
OVER_ENVELOPE_EXTENTS = (400.0, 20.0, 20.0)


def _units_list(result: dict) -> list[str]:
    return [opt["unit"] for opt in result["options"]]


# ---------------------------------------------------------------------------
# plausible STL → assumed mm
# ---------------------------------------------------------------------------


def test_plausible_stl_assumed_mm() -> None:
    result = classify_stl_units(BOX_20MM_EXTENTS)
    assert result["status"] == "assumed"
    assert result["unit"] == "mm"
    assert result["scale"] == 1.0


def test_plausible_stl_fixture_assumed_mm() -> None:
    """The imported-part fixture's known extents settle to mm by assumption."""
    result = classify_stl_units(BOX_20MM_EXTENTS)
    assert result["status"] == "assumed"
    assert result["scale"] == pytest.approx(1.0)


def test_just_at_five_mm_is_plausible() -> None:
    # The classification boundary: largest side >= 5 mm is plausible.
    result = classify_stl_units((5.0, 2.0, 3.0))
    assert result["status"] == "assumed"
    assert result["scale"] == 1.0


# ---------------------------------------------------------------------------
# implausible STL → unsettled, options likeliest first
# ---------------------------------------------------------------------------


def test_over_envelope_stl_unsettled() -> None:
    result = classify_stl_units(OVER_ENVELOPE_EXTENTS)
    assert result["status"] == "unsettled"
    assert result["unit"] is None
    assert result["scale"] is None
    assert result["options"] is not None


def test_over_envelope_stl_inch_is_likeliest_first() -> None:
    # 400 file units as mm overflows the 320 mm envelope, but 400 inches is
    # absurdly large and 400 cm fits — yet within the "good" tier the
    # priority is inch > cm > mm; the over-envelope mm reading means no
    # option is good, so the pure priority order (inch, cm, mm) holds.
    result = classify_stl_units(OVER_ENVELOPE_EXTENTS)
    assert _units_list(result) == ["inch", "cm", "mm"]


def test_tiny_stl_unsettled_with_tiered_options() -> None:
    # 2 file units: cm reading (20 mm) fits the envelope and is >= 5 mm;
    # inch reading (50.8 mm) too — so both are "good" and come first in
    # priority (inch > cm), with the mm reading (2 mm, below 5 mm) last.
    result = classify_stl_units((2.0, 2.0, 2.0))
    assert result["status"] == "unsettled"
    assert _units_list(result) == ["inch", "cm", "mm"]
    good = [o for o in result["options"] if o["fits_envelope"] and o["at_least_5mm"]]
    assert {o["unit"] for o in good} == {"inch", "cm"}
    mm_only = [o for o in result["options"] if o["unit"] == "mm"]
    assert mm_only[0]["at_least_5mm"] is False


def test_unsettled_option_extents_are_scaled_mm() -> None:
    result = classify_stl_units((2.0, 4.0, 3.0))
    assert result["status"] == "unsettled"
    for opt in result["options"]:
        if opt["unit"] == "mm":
            assert opt["extents_mm"] == [2.0, 4.0, 3.0]
        elif opt["unit"] == "cm":
            assert opt["extents_mm"] == [20.0, 40.0, 30.0]
        elif opt["unit"] == "inch":
            assert opt["extents_mm"] == [50.8, 101.6, pytest.approx(76.2)]


# ---------------------------------------------------------------------------
# 3MF is never asked: absent unit → mm (scale 1.0)
# ---------------------------------------------------------------------------


def test_3mf_absent_unit_never_asked() -> None:
    assert mm_factor_for_unit(None) == 1.0


def test_3mf_declared_units_convert() -> None:
    assert mm_factor_for_unit("mm") == 1.0
    assert mm_factor_for_unit("cm") == 10.0
    assert mm_factor_for_unit("inch") == 25.4


def test_3mf_unknown_unit_is_error_not_assumed_mm() -> None:
    with pytest.raises(PartUploadError):
        mm_factor_for_unit("parsec")
