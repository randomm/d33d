"""The name/value primitives shared by the design-state block and the stated-feature matcher (issue #447).

A LEAF module: it imports nothing from ``d33d`` (issue #447). Both
``d33d.design_state`` and ``d33d.feature_match`` need these two
primitives, and ``design_state`` imports ``feature_match`` at module top,
so neither primitive can live in ``design_state`` — the matcher importing
them back would close a cycle, and the AC forbids a lazy import to work
around one. ``d33d.design_loop`` also reaches ``_split_name_tokens`` for
its feature-param exemption (issue #420); it resolves through
``design_state``'s re-export, which is this module's function object.

Two primitives, both stateless and dependency-free:

- :func:`is_number` — the project's numeric check. ``bool`` is excluded:
  ``True`` is an int in Python but it is never a measurement, and a
  stated feature size or a param value that is a bool must not take part
  in arithmetic.
- :func:`split_name_tokens` — SCAD identifier → lowercase tokens. Split
  on ``_``, then each piece on camelCase boundaries (``draftAngle`` →
  ``["draft", "angle"]``). Empty pieces dropped. Both the unit inference
  (``_infer_unit``) and the feature matcher's token source are built on
  it, so a name tokenises the same way in both.
"""

from __future__ import annotations

from typing import Any


def is_number(value: Any) -> bool:
    """True for int/float (bool is excluded — it is not a measurement)."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def split_name_tokens(name: str) -> list[str]:
    """The name's tokens: split on ``_``, each piece on camelCase
    boundaries (``draftAngle`` → ``["draft", "angle"]``). Empty dropped."""
    pieces: list[str] = []
    for raw in name.split("_"):
        if not raw:
            continue
        token = ""
        for i, ch in enumerate(raw):
            if ch.isupper() and i > 0:
                pieces.append(token.lower())
                token = ""
            token += ch
        if token:
            pieces.append(token.lower())
    return pieces
