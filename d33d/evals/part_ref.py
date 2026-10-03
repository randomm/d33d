"""The imported-part ref schema (issue #340).

The ``imported_part`` cases pin the mesh fixture they render against.
This module owns :class:`PartRef` (the fixture path + settled scale)
and :func:`part_of` (the typed accessor on a case). It lives in its
own file so the imported-part schema pieces stay separate from the
case schema (which imports :class:`PartRef` at module level only
because :class:`~d33d.evals.case_schema.GoldenCase` needs it — no
circular import, since this module imports ``case_schema`` under
``TYPE_CHECKING``).
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field, field_validator

if TYPE_CHECKING:
    from d33d.evals.case_schema import GoldenCase


class PartRef(BaseModel):
    """The imported-part mesh a case renders against (issue #340).

    ``fixture`` is a path under ``evals/cases/fixtures/`` (relative to the
    repo root); ``scale`` is the settled file→mm factor the import guard
    accepts for this case (1.0 for an mm part — the guard compares
    numerically within ``SCALE_FACTOR_TOL``, never by string).
    """

    fixture: str = Field(min_length=1)
    # ``gt=0`` alone also rejects NaN (NaN < 0 is False) — the finite
    # check below stays for inf, and as the one place the message says
    # so explicitly.
    scale: float = Field(gt=0)

    @field_validator("scale")
    @classmethod
    def _scale_must_be_finite(cls, v: float) -> float:
        if not math.isfinite(v):
            raise ValueError("scale must be a finite positive number")
        return v


def part_of(case: GoldenCase) -> PartRef:
    """The part ref of an ``imported_part`` case.

    Returns ``case.part``, or raises :class:`ValueError` when it is
    ``None`` — unreachable for a schema-validated ``imported_part``
    case (the model validator requires the ref), but a typed error
    instead of an ``AttributeError`` on ``None`` if that invariant is
    ever broken by a caller constructing cases out-of-band.
    """
    if case.part is None:
        raise ValueError(f"case {case.case_id!r} has no part ref")
    return case.part
