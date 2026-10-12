"""One-decimal millimetre formatting shared by the offer copy (issue #437).

Diameters are rendered with exactly one decimal place, rounded half-up
(``3.25`` → ``3.3``, ``38`` → ``38.0``). The TypeScript twin is ``dia()``
in ``web/src/copy.ts``; the shared sample values are pinned on both sides.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal


def fmt_mm1(value: float) -> str:
    """``value`` as a one-decimal string, half-up (never binary rounding).

    ``repr`` gives the shortest round-tripping decimal, so the rounding
    works on the number the user typed, not on its binary approximation.
    """
    quantized = Decimal(repr(float(value))).quantize(
        Decimal("0.1"), rounding=ROUND_HALF_UP
    )
    return f"{quantized:f}"
