"""Part unit handling for the STL/3MF import (issue #325).

One source for the file-units→mm scale, shared by the 3MF import path
(the file's ``<model unit>`` attribute), the STL classification, and the
settle endpoint (``{"unit"}`` / ``{"axis","mm"}`` bodies):

- ``mm_factor_for_unit`` — the mm factor for a declared unit (the closed
  mm/cm/inch synonym sets). Only an ABSENT unit (``None``) means the 3MF
  default (millimeters → 1.0); a non-string unit, or a string outside the
  sets, is a ``PartUploadError`` (the 422) — never a silent mm assumption.
- ``classify_stl_units`` — the STL classification (STL carries no units:
  plausible → assumed mm; otherwise unsettled with the candidate options,
  likeliest first).
- ``settle_scale`` — the settle endpoint's ``{"unit"}`` scale (``mm``/
  ``cm``/``inch`` → 1 / 10 / 25.4).
"""

from __future__ import annotations

from typing import Any

from d33d.part_mesh import PartUploadError
from d33d.print_validation import QIDI_PLUS_5_ENVELOPE_MM

#: 3MF unit values (from the file's ``<model unit="...">`` attribute) per
#: scale. A declared unit outside these closed sets is unconvertible — a
#: clean 422 (the ``_force_mm`` precedent), never a silent mm assumption.
_MM_UNIT_VALUES = {"mm", "millimeter", "millimeters", "millimetre", "cmm"}
_CM_UNIT_VALUES = {"cm", "centimeter", "centimeters", "centimetre"}
_INCH_UNIT_VALUES = {"in", "inch", "inches"}

#: The settle endpoint's closed unit set and its fixed scales (file
#: units → mm): the single source the route's ``{"unit"}`` branch reads.
_SETTLE_UNIT_SCALES = {"mm": 1.0, "cm": 10.0, "inch": 25.4}

#: The candidate units offered while an STL is unsettled (in priority
#: order for the "likeliest first" sort: inch > cm > mm).
_CANDIDATE_UNITS = ("inch", "cm", "mm")

#: Axis lexicon → bbox component index (W→x, D→y, H→z).
_AXIS_TO_INDEX = {"W": 0, "D": 1, "H": 2}


def mm_factor_for_unit(unit: Any) -> float:
    """The file-units→mm factor for a declared unit.

    - ``None`` (an ABSENT unit — a 3MF with no ``unit`` attribute) → 1.0
      (the 3MF default is millimeters);
    - a string in the closed mm/cm/inch synonym sets → 1.0 / 10.0 / 25.4;
    - anything else (a non-string value, or a string outside the sets) →
      ``PartUploadError`` (the 422 — never a silent mm assumption).
    """
    if unit is None:
        return 1.0
    if not isinstance(unit, str):
        raise PartUploadError(f"mesh has a non-string unit {unit!r}")
    u = unit.strip().lower()
    if u in _MM_UNIT_VALUES:
        return 1.0
    if u in _CM_UNIT_VALUES:
        return 10.0
    if u in _INCH_UNIT_VALUES:
        return 25.4
    raise PartUploadError(f"unconvertible 3MF unit {unit!r}")


def settle_scale(unit: str) -> float:
    """The file-units→mm scale for a settle-by-unit body (``mm``/``cm``/
    ``inch`` → 1 / 10 / 25.4). The caller validates ``unit`` is in the
    closed set (``settle_unit_choices``)."""
    return _SETTLE_UNIT_SCALES[unit]


def settle_unit_choices() -> tuple[str, ...]:
    """The settle endpoint's closed unit set (``mm``/``cm``/``inch``)."""
    return tuple(_SETTLE_UNIT_SCALES)


def axis_to_index() -> dict[str, int]:
    """Axis lexicon → bbox component index (W→x, D→y, H→z)."""
    return dict(_AXIS_TO_INDEX)


def _fits_envelope(extents_mm: tuple[float, float, float]) -> bool:
    """True when every axis fits the QIDI X-Plus 5 build envelope (the
    named constant — the same value gate 7 reads; never a copy of the
    numbers)."""
    env = QIDI_PLUS_5_ENVELOPE_MM
    return all(e <= env[i] for i, e in enumerate(extents_mm))


def _candidate_option(
    unit: str, scale: float, file_extents: tuple[float, float, float]
) -> dict[str, Any]:
    """One candidate unit option: the unit word, its scale, and the
    resulting mm extents (file units × scale)."""
    mm = [e * scale for e in file_extents]
    largest = max(mm)
    return {
        "unit": unit,
        "scale": scale,
        "extents_mm": mm,
        "fits_envelope": _fits_envelope(tuple(mm)),
        "at_least_5mm": largest >= 5.0,
    }


def classify_stl_units(
    file_extents: tuple[float, float, float],
) -> dict[str, Any]:
    """The STL unit classification (STL carries no units):

    - largest side >= 5 mm AND the mm-reading fits the envelope →
      ``{"status": "assumed", "unit": "mm", "scale": 1.0}`` (shown as
      assumed — the user can still change it);
    - otherwise ``{"status": "unsettled", "options": [...]}`` with the
      candidate units (mm, cm, inch) each carrying its resulting mm
      extents, likeliest first: those whose largest mm side is >= 5 AND
      fits the envelope come first, in priority inch > cm > mm; the rest
      follow in the same priority.
    """
    mm_extents = file_extents
    largest_mm = max(mm_extents)
    if largest_mm >= 5.0 and _fits_envelope(mm_extents):
        return {"status": "assumed", "unit": "mm", "scale": 1.0, "options": None}
    options = [
        _candidate_option(unit, settle_scale(unit), file_extents)
        for unit in _CANDIDATE_UNITS
    ]

    def _rank(opt: dict[str, Any]) -> tuple[int, int]:
        good = 1 if (opt["fits_envelope"] and opt["at_least_5mm"]) else 0
        # inch=0, cm=1, mm=2 — the priority within each tier.
        priority = _CANDIDATE_UNITS.index(opt["unit"])
        return (-good, priority)

    options.sort(key=_rank)
    return {"status": "unsettled", "unit": None, "scale": None, "options": options}


__all__ = [
    "axis_to_index",
    "classify_stl_units",
    "mm_factor_for_unit",
    "settle_scale",
    "settle_unit_choices",
]
