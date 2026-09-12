"""Eval harness gate 6 — slice dry run (issue #9, workstream task-slice).

Gate 6 is DELEGATED to the print-validation pipeline (ticket #4). The eval
harness never implements its own slicer call — it shells into
``d33d.print_validation.validate_stl`` with a custom
``slice_dry_run_fn`` (default: the real ``d33d.slicer.slice_dry_run``).

The single, testable N/A trigger (per the resolved open question in
issue #9): the gate reports N/A if and only if ``slicer.available_slicers()``
returns an empty dict — i.e. no QIDI, Orca, or PrusaSlicer binary is
invocable. A partial configuration (e.g. PrusaSlicer present but QIDI
absent) is NOT N/A: the driver's own precedence/fallback logic runs and
the gate passes or fails on the driver's verdict.

The N/A status is its own closed enum value (``"na"``) — never a pass,
never a hard fail — so the report can distinguish "slicer not installed
on this box" from "the model produced a mesh that won't slice".
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from d33d import slicer

#: Stable label for gate 6 in the per-case, per-gate report.
SLICE_GATE_LABEL: str = "slice_dry_run"

#: Human-readable reason string for the N/A outcome.
NA_REASON: str = "N/A — slicer not available headless"


@dataclass(frozen=True)
class SliceGateResult:
    """Outcome of gate 6 for one eval case.

    ``status`` is one of:
      - ``"pass"`` — a slicer was available and the dry run succeeded.
      - ``"fail"`` — a slicer was available and the dry run failed;
        ``error_class`` is ``"slice"`` (the #4 pipeline's closed enum
        value for this gate) and ``slicer``/``detail`` carry the
        driver's diagnosis.
      - ``"na"``   — no slicer binary invocable (see :data:`NA_REASON`);
        ``error_class`` is ``None`` and ``slicer`` is ``None``.
    """

    status: Literal["pass", "fail", "na"]
    ok: bool
    error_class: str | None
    slicer: str | None
    detail: str


def run_slice_gate(
    stl_path: str,
    slice_fn: slicer.SliceDryRunResult | None = None,
) -> SliceGateResult:
    """Run gate 6 on an STL, delegating to ``d33d.slicer``.

    The driver is injected (``slice_fn``) so the eval harness can point
    at a stub or the real ``slicer.slice_dry_run`` without changing
    this module. The N/A check reads ``slicer.available_slicers()``
    directly, so the trigger is testable without monkey-patching the
    driver itself.
    """
    # Single N/A trigger: no slicer binary at all.
    if not slicer.available_slicers():
        return SliceGateResult(
            status="na",
            ok=False,
            error_class=None,
            slicer=None,
            detail=NA_REASON,
        )

    if slice_fn is not None and not callable(slice_fn):
        raise TypeError("slice_fn must be callable or None")
    driver = slice_fn if slice_fn is not None else slicer.slice_dry_run
    raw = driver(stl_path, None)
    if raw.ok:
        return SliceGateResult(
            status="pass",
            ok=True,
            error_class=None,
            slicer=raw.slicer,
            detail=raw.detail,
        )
    return SliceGateResult(
        status="fail",
        ok=False,
        error_class="slice",
        slicer=raw.slicer,
        detail=raw.error_string or raw.detail,
    )
