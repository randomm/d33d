"""The import-aware post-check for the design loop (issue #332, sub-issue 3).

The deterministic post-check that runs on the ok-render branch of the design
loop for a project whose part is assumed/settled: EVERY candidate on an
import project must build on the imported part — it must
``import("part.stl")`` (the worker seeds the committed mesh, 3MF converted
on the host, under that fixed name) and it must never resize the import (a
``scale()`` on the import other than the settled file→mm factor, or a
``resize()`` anywhere).

The module mirrors the #317 ``screw_hole_check`` pattern: the loop
orchestrates, this module owns the detection. :func:`import_guard_violation`
returns a DETECTION tuple ``(reason, detail)`` — the caller
(``d33d.design_loop``) folds it into the EXISTING ``geometrically_wrong``
repair dict (no new error_class, no new dict shape) and routes it through
the same ``route_repair`` / ``_design_messages`` path as every other repair.

The check binds transforms to the ``import`` node, never string-matches
``scale(``/``resize(`` anywhere: the model legitimately scales geometry it
adds (normalising a new feature) or cuts the import with ``difference()``.
Only a ``scale()``/``resize()`` applied to the ``import("part.stl")``
expression itself is a violation, and so is importing a DIFFERENT filename.
"""

from __future__ import annotations

import re

__all__ = [
    "PART_STL_NAME",
    "SCALE_FACTOR_TOL",
    "import_guard_violation",
    "part_import_call",
    "part_scale_call",
]

#: The single filename the render worker seeds the committed part as
#: (issue #330: 3MF is converted to STL on the host, so the import path in
#: the .scad is always ``part.stl`` regardless of the original upload
#: format — the guard checks this name and never ``part.3mf``).
PART_STL_NAME = "part.stl"

#: The float tolerance for the settled scale comparison (operator decision
#: for issue #332: the settled factor is accepted within a relative
#: tolerance of ``SCALE_FACTOR_TOL`` of the factor, so ``scale(25.400001)``
#: for a settled factor of 25.4 passes and any other factor rejects —
#: numeric, not string, comparison).
SCALE_FACTOR_TOL = 1e-6

#: The ``import`` call of the candidate, any number of args, any
#: whitespace (``import("part.stl")`` / ``import( "part.stl" )``).
_IMPORT_RE = re.compile(r'\bimport\s*\(([^)]*)\)')
#: A quoted string argument — single or double, no escapes (a mesh path
#: never carries a quote).
_QUOTED_ARG_RE = re.compile(r'^["\']([^"\']+)["\']$')
#: ``scale(...)`` with the raw argument text.
_SCALE_RE = re.compile(r"\bscale\s*\(([^)]*)\)")
#: ``resize(...)`` with the raw argument text.
_RESIZE_RE = re.compile(r"\bresize\s*\(([^)]*)\)")
#: A numeric literal inside an argument list (the scale factor / resize
#: vector elements).
_NUM_RE = re.compile(r"\b(\d+(?:\.\d*)?|\.\d+)\b")


def _parse_quoted_arg(raw: str) -> str | None:
    """The quoted path ``import(...)`` names, or ``None`` when the argument
    is not a single quoted string (an expression / list argument — the
    import guard does not interpret it, it simply is not the fixed
    ``part.stl`` reference)."""
    m = _QUOTED_ARG_RE.match(raw.strip())
    return m.group(1) if m else None


def part_import_call(scad_source: str) -> str | None:
    """The filename the candidate ``import``s, or ``None`` when the source
    imports nothing.

    One-pass over the ``import`` calls: the first call whose argument is a
    single quoted string wins (a source with several distinct imports is
    degenerate — the first named one is what the guard reports, and a
    second import of a different name is caught as ``wrong_import`` either
    way it is listed). ``None`` is the honest absence (no import at all).
    """
    for raw in _IMPORT_RE.findall(scad_source):
        name = _parse_quoted_arg(raw)
        if name is not None:
            return name
    return None


def _factors_in(raw: str) -> list[float]:
    """The numeric factors in one transform's argument list (empty for a
    symbolic argument like ``[k, k, 1]``)."""
    out: list[float] = []
    for literal in _NUM_RE.findall(raw):
        try:
            out.append(float(literal))
        except ValueError:
            continue
    return out


def _scale_ok(raw: str, part_scale: float) -> bool:
    """True iff every numeric factor in ``scale(...)`` equals the settled
    file→mm factor within ``SCALE_FACTOR_TOL`` (numeric, never string
    equality). A scale with no numeric factors (a symbolic ``[k, k, 1]``)
    cannot be verified as a resize and passes — the bbox gate is the
    backstop, not this guard."""
    factors = _factors_in(raw)
    if not factors:
        return True
    return all(abs(f - part_scale) <= SCALE_FACTOR_TOL * max(1.0, abs(part_scale)) for f in factors)


def part_scale_call(scad_source: str, part_scale: float) -> tuple[bool, str | None]:
    """The import's own transforms, scanned over the candidate source.

    Returns ``(ok, detail)``: ``ok`` is False (with a ``detail`` naming the
    offending transform) when the source ``resize()``s anything, or applies
    a ``scale()`` to the ``import(...)`` expression itself with a factor
    other than the settled one. A ``scale()`` NOT bound to the import
    (scaling geometry the model adds) is never a violation.

    Binding rule: the ``scale``/``resize`` call that IMMEDIATELY (ignoring
    whitespace) precedes an ``import(...)`` call is the one applied to the
    import (``scale(10) import("part.stl")`` — transform-then-call order,
    the only shape in which a transform reaches the imported mesh, because
    the import is a child of the transform). A ``resize()`` anywhere is
    unconditionally a violation (``resize`` is only meaningful on
    children, and the model is never given a legitimate reason to resize
    anything in this protocol).
    """
    import_spans = [(m.start(), m.end()) for m in _IMPORT_RE.finditer(scad_source)]
    if not import_spans:
        # The "no import at all" violation is the caller's (it reports it
        # first); without an import there is nothing to bind a scale to.
        return True, None
    for m in _RESIZE_RE.finditer(scad_source):
        return False, f"resize({m.group(1)})"
    for m in _SCALE_RE.finditer(scad_source):
        # A scale binds to the import when it directly precedes an import
        # call (whitespace only between the closing paren and ``import``)
        # — the transform's child is the imported mesh. The source slice is
        # computed once per scale (the span list is shared).
        binds_to_import = any(
            scad_source[end : m.start()].strip() == "" for _, end in import_spans
        )
        if binds_to_import and not _scale_ok(m.group(1), part_scale):
            return False, f"scale({m.group(1)})"
    return True, None

def import_guard_violation(
    scad_source: str, *, part_scale: float
) -> tuple[str, str] | None:
    """The import guard's detection over one ok-render candidate (issue
    #332): the DETECTION tuple ``(reason, detail)``, or ``None`` when the
    candidate obeys the import contract (the loop abstains — the other
    gates and post-checks decide).

    Violations, in check order:

    * ``no_import`` — the candidate does not ``import("part.stl")`` at all
      (on an import project the part is the base of EVERY candidate, on
      any turn type — the operator's binding decision);
    * ``wrong_import`` — it imports a different filename (never
      ``part.3mf``: the worker converts 3MF to STL on the host, the
      seeded name is always ``part.stl``);
    * ``rescaled_import`` — it ``scale()``s the import with a factor other
      than the settled file→mm one (compared numerically within
      ``SCALE_FACTOR_TOL``);
    * ``resized_part`` — it ``resize()``s anything (the import's features
      are mesh geometry — no parameters to resize, ever).

    The detail carries the offending text for the repair's ``evidence``
    (the caller renders it into the ``geometrically_wrong`` directive —
    the existing class, no new error_class).
    """
    name = part_import_call(scad_source)
    if name is None:
        return "no_import", "the candidate does not import(" + repr(PART_STL_NAME) + ")"
    if name != PART_STL_NAME:
        return "wrong_import", f"the candidate imports {name!r}, not {PART_STL_NAME!r}"
    ok, detail = part_scale_call(scad_source, part_scale)
    if not ok:
        if "resize(" in (detail or ""):
            return "resized_part", f"the candidate {detail} on the part"
        return "rescaled_import", (
            f"the candidate {detail} on the import, not scale({part_scale:g})"
        )
    return None
