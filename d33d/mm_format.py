"""One-decimal millimetre formatting shared by the offer copy (issue #437).

Diameters are rendered with exactly one decimal place, rounded half-up
(``3.25`` → ``3.3``, ``38`` → ``38.0``). The TypeScript twin is ``fmtMm1``
in ``web/src/copy.ts``; the shared sample values are pinned on both sides.
"""

from __future__ import annotations

import math
from decimal import ROUND_HALF_UP, Decimal, localcontext

#: Enough significant digits for any finite float (309 integer digits plus
#: the one decimal place); the default context's 28 raises on large values.
_PRECISION = 400


def fmt_mm1(value: float) -> str:
    """``value`` as a one-decimal string, half-up (never binary rounding).

    ``repr`` gives the shortest round-tripping decimal, so the rounding
    works on the number the user typed, not on its binary approximation.
    A non-finite value has no decimal form and is rendered as ``inf`` /
    ``-inf`` / ``nan`` (the trigger never passes one).
    """
    number = float(value)
    if not math.isfinite(number):
        return f"{number:g}"
    with localcontext() as ctx:
        ctx.prec = _PRECISION
        quantized = Decimal(repr(number)).quantize(
            Decimal("0.1"), rounding=ROUND_HALF_UP
        )
    return f"{quantized:f}"
