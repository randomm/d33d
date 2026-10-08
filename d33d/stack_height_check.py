"""Deterministic stack-height post-check (issue #409).

The post-check runs on the ok-render branch of the design loop, beside
the #317 screw-hole and #386 through-hole post-checks, BEFORE the
``score.perfect`` pass return. The v65 repro: a lid written as a stack
of height-named features (plate 4 + rim drop 2 + skirt 5 = 11 mm) where
the model committed the stack with ``difference()`` instead of a union
— the features subtracted from a plate they don't touch, so the render
is a flat 55×40×4 slab while the model's own declared stack
(``skirt_total_height = base_height + skirt_height``) says 11 mm. The
defect is invisible to every gate bit (no stated H, no H-tagged
param), so without this check a discarded stack passes silently.

The check (``stack_height_check``) compares a DECLARED derived height
sum to the measured Z (the bbox the loop already computes): in the
SCAD's top-level parameter block, find a height-named derived
parameter whose value is a sum of at least two parameters that each
resolve to a positive constant, and fire when the declared sum and the
measured Z disagree by MORE than the disagrees-major threshold
(``max(20% of declared, 5 mm)`` — the same pair as bit 5).

The module is deliberately separate from ``d33d.design_loop`` (the
loop orchestrates; this module owns the check's logic), mirroring
``d33d.screw_hole_check`` / ``d33d.through_hole_check`` (detection-only
module returning a tuple or ``None``, the caller folding it into the
repair dict). The deferred-import wrapper lives in ``design_loop``
(the module cannot import ``design_prompts`` at top level — the #317
circular-import break applies the same way).
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

__all__ = [
    "STACK_HEIGHT_INSTRUCTION",
    "declared_stack_sum",
    "is_height_name",
    "stack_height_check",
]

#: Name tokens that mark a HEIGHT parameter (issue #409, operator
#: decision 2026-10-05). A declaration's name qualifies when ANY of its
#: ``_split_name_tokens`` pieces (``design_state`` — snake_case +
#: camelCase split, reused) is in this set. ``base`` (the stack's base
#: sub-total, v65's ``base_height = lid_base_thickness + rim_drop``)
#: and the feature words (``skirt``/``rim``/``lip``/``drop``) qualify
#: alongside the generic length words.
_HEIGHT_TOKENS: frozenset[str] = frozenset(
    (
        "height",
        "thickness",
        "drop",
        "skirt",
        "rim",
        "lip",
        "base",
        "total",
        "overall",
    )
)

#: One ``name = rhs;`` parameter declaration (any RHS — the loop's own
#: ``_DECLARATION_RE`` captures literal-RHS declarations only; this
#: check's candidates are DERIVED sums, which need a broader pattern).
_DECL_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*([^;]*);", re.MULTILINE)

#: An ``// ...`` line comment or a ``/* ... */`` block comment (comments
#: are stripped before scanning — a ``// 65`` inside a comment must
#: never read as a value, and ``;`` inside a comment must never split
#: a declaration).
_COMMENT_RE = re.compile(r"//[^\n]*|/\*.*?\*/", re.DOTALL)

#: A bare ``"..."`` string literal (never appears in a parameter block
#: in practice, but stripped anyway — an unbalanced quote would shift
#: the bracket-balance check).
_STRING_RE = re.compile(r'"[^"]*"')

#: A decimal literal (``4``, ``1.5``, ``-0.5``, ``2.e1``).
_NUMBER_RE = re.compile(r"-?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?")


def _split_name_tokens(name: str) -> list[str]:
    """The name's tokens: split on ``_``, each piece on camelCase
    boundaries (``draftAngle`` → ``["draft", "angle"]``) — the same
    tokenisation ``d33d.design_state._infer_unit`` (issue #390) uses,
    inlined here to keep the check module free of a design_state import
    (the design_loop → design_state chain is already established, but
    the check reads only names, so a private local copy of a two-line
    helper keeps the module's imports to stdlib + the deferred
    ``design_loop`` seam only)."""
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


def is_height_name(name: str) -> bool:
    """Does this parameter NAME read as a height?

    True when any of the name's tokens (``_split_name_tokens`` pieces)
    is in :data:`_HEIGHT_TOKENS` — ``skirt_total_height``,
    ``base_height``, ``lid_base_thickness``, ``overall_height`` all
    qualify; ``lid_width``, ``rim_width`` do not (``width`` is not a
    height token). A name with no tokens (``_``) is not a height name.
    """
    return any(t in _HEIGHT_TOKENS for t in _split_name_tokens(name))


def _strip(scad: str) -> str:
    """Comments blanked (newlines preserved — declaration line
    boundaries survive), string literals emptied."""
    s = _COMMENT_RE.sub(
        lambda m: ("\n" * m.group(0).count("\n") or " "), scad
    )
    return _STRING_RE.sub('""', s)


def _eval_atom(expr: str, env: dict[str, float]) -> float | None:
    """An atom: a parenthesized expression (fully balanced), a decimal
    literal, or a bare identifier in ``env``. ``None`` otherwise —
    function calls, strings, vectors are not readable atoms."""
    e = expr.strip()
    if e.startswith("(") and e.endswith(")"):
        depth = 0
        ok = True
        for i, ch in enumerate(e):
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth < 0:
                    ok = False
                    break
            if not ok:
                break
        if not ok or depth != 0:
            return None
        return _eval_add(e[1:-1], env)
    if _NUMBER_RE.fullmatch(e):
        return float(e)
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", e) and e in env:
        return env[e]
    return None


def _eval_mul(expr: str, env: dict[str, float]) -> float | None:
    """One level of the precedence chain: ``*``/``/`` (and a unary
    minus) over :func:`_eval_atom` operands."""
    e = expr.strip()
    if e.startswith("-") and not _NUMBER_RE.fullmatch(e):
        inner = _eval_mul(e[1:], env)  # recurse: `--a` stays in this level
        return -inner if inner is not None else None
    depth = 0
    splits: list[int] = []
    for i, ch in enumerate(e):
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth < 0:
                return None
        elif depth == 0 and ch in "*/":
            splits.append(i)
    if not splits:
        return _eval_atom(e, env)
    parts = [e[: splits[0]]]
    for a, b in zip(splits, splits[1:]):
        parts.append(e[a + 1 : b])
    parts.append(e[splits[-1] + 1 :])
    acc = _eval_atom(parts[0], env)
    if acc is None:
        return None
    for op, part in zip(splits, parts[1:]):
        r = _eval_atom(part, env)
        if r is None:
            return None
        if e[op] == "*":
            acc *= r
        else:
            if r == 0:
                return None
            acc /= r
    return acc


def _eval_add(expr: str, env: dict[str, float]) -> float | None:
    """One level of the precedence chain: top-level ``+``/``-`` over
    :func:`_eval_mul` operands (a leading unary minus is not a split)."""
    depth = 0
    splits: list[tuple[int, str]] = []
    for i, ch in enumerate(expr):
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth < 0:
                return None
        elif depth == 0 and ch in "+-":
            if i == 0:
                continue  # a leading unary minus belongs to the operand
            splits.append((i, ch))
    if not splits:
        return _eval_mul(expr, env)
    parts: list[str] = [expr[: splits[0][0]]]
    ops: list[str] = []
    for k, (i, op) in enumerate(splits):
        end = splits[k + 1][0] if k + 1 < len(splits) else len(expr)
        parts.append(expr[i + 1 : end])
        ops.append(op)
    acc = _eval_mul(parts[0], env)
    if acc is None:
        return None
    for op, part in zip(ops, parts[1:]):
        r = _eval_mul(part, env)
        if r is None:
            return None
        acc = acc + r if op == "+" else acc - r
    return acc


def _bare_params_in(rhs: str, env: dict[str, float]) -> tuple[str, ...]:
    """The DISTINCT bare parameter references in ``rhs`` — identifier
    spans that are exactly a known parameter name and are NOT part of a
    number (a ``2`` inside ``rim_width`` never counts). Each span must
    resolve to a STRICTLY positive value (a zero or negative parameter
    is not a stack component).

    An operand containing ANY arithmetic is NOT a bare reference
    (``2*rim_width`` is a term, not a parameter) — the span must match
    a name in full.
    """
    out: list[str] = []
    seen: set[str] = set()
    for m in re.finditer(r"[A-Za-z_][A-Za-z0-9_]*", rhs):
        name = m.group(0)
        if name not in env or name in seen:
            continue
        seen.add(name)
        if env[name] > 0:
            out.append(name)
    return tuple(out)


def declared_stack_sum(scad_source: str) -> tuple[float, tuple[str, ...]] | None:
    """The model's DECLARED overall-height stack in the SCAD source.

    Evaluate the top-level parameter block (every ``name = rhs;``
    declaration, in order, with a small precedence evaluator over
    literals and previously-declared names), then find every
    height-named derived parameter whose RHS is a genuine sum of at
    least two distinct positive parameters:

    * the RHS must contain a top-level ``+``;
    * the RHS must evaluate to a finite value;
    * the name must be height-named (:func:`is_height_name`);
    * the value must be strictly positive;
    * the RHS must reference at least two DISTINCT parameters (bare
      names — operands containing arithmetic, e.g. ``2*rim_width``,
      do not count);
    * the evaluated value must equal the sum of those references
      (``a = b + c - c`` is not a stack sum).

    When several qualify, the LARGEST wins (the declared overall height
    — a part's total outranks its sub-totals). ``None`` when no such
    sum is readable (no declarations, unbalanced brackets, or a
    declaration with an unreadable RHS — the abstain cases: a fabricated
    stack is worse than no stack).
    """
    s = _strip(scad_source)
    for open_c, close_c in (("(", ")"), ("[", "]"), ("{", "}")):
        if s.count(open_c) != s.count(close_c):
            logger.info(
                "stack-height check abstained: unbalanced %s in parameter block",
                open_c,
            )
            return None
    decls: dict[str, str] = {}
    for m in _DECL_RE.finditer(s):
        name, rhs = m.group(1), m.group(2).strip()
        if name in decls:
            logger.info(
                "stack-height check abstained: parameter %r declared twice", name
            )
            return None
        decls[name] = rhs
    if not decls:
        logger.info("stack-height check abstained: no parameter block")
        return None

    env: dict[str, float] = {}
    for name, rhs in decls.items():
        value = _eval_add(rhs, env)
        if value is None or value != value or value in (float("inf"), float("-inf")):
            logger.info(
                "stack-height check abstained: declaration %r does not resolve "
                "to a constant",
                name,
            )
            return None
        env[name] = value

    best: tuple[float, tuple[str, ...]] | None = None
    for name, rhs in decls.items():
        if "+" not in rhs:
            continue
        if not is_height_name(name):
            continue
        value = env[name]
        if not value > 0:
            continue
        refs = _bare_params_in(rhs, env)
        if len(refs) < 2:
            continue
        if abs(value - sum(env[r] for r in refs)) > 1e-9:
            continue
        if best is None or value > best[0]:
            best = (value, refs)
    if best is None:
        logger.info("stack-height check abstained: no declared height sum")
    return best


#: The stack-height post-check's repair instruction (issue #409): the
#: part's measured height does not match its own declared stack — a
#: stacked feature was discarded by a wrong CSG operator (``difference``
#: where the stack belongs in a union), or a component was not modeled.
STACK_HEIGHT_INSTRUCTION = (
    "the part's measured height does not match the declared stack sum — "
    "model every declared component so the part's overall height matches "
    "the declared sum (and use union, not difference, to combine stacked "
    "parts)."
)


def stack_height_check(
    scad_source: str,
    measured_z: float | None,
) -> tuple[float, float] | None:
    """The stack-height post-check (issue #409).

    Runs on the ok-render branch of the loop, beside the #317 and #386
    post-checks and BEFORE the ``score.perfect`` pass return. Returns
    ``None`` (the check abstains — nothing is fed to the next iteration)
    when the declared stack is unreadable (:func:`declared_stack_sum`)
    or ``measured_z`` is missing (no mesh / no bbox).

    Otherwise returns the DETECTION tuple ``(declared, measured)`` when
    ``abs(declared − measured_z)`` exceeds the disagrees-major
    threshold ``max(20% of declared, 5 mm)`` (the same pair as bit 5 —
    ``d33d.design_state.DISAGREES_MAJOR_THRESHOLD_REL`` /
    ``DISAGREES_MAJOR_THRESHOLD_MIN_MM``); a diff AT the threshold
    passes (strict ``>``, mirroring bit 5). The caller (
    ``d33d.design_loop``) folds this into the repair dict — the EXISTING
    ``geometrically_wrong`` class, no new class — and routes it through
    ``route_repair``.
    """
    stack = declared_stack_sum(scad_source)
    if stack is None:
        return None
    declared, _refs = stack
    if not isinstance(measured_z, (int, float)) or isinstance(measured_z, bool):
        logger.info("stack-height check abstained: no measured Z (no mesh or bbox)")
        return None
    measured = float(measured_z)
    from d33d.design_state import (
        DISAGREES_MAJOR_THRESHOLD_MIN_MM,
        DISAGREES_MAJOR_THRESHOLD_REL,
    )

    tol = max(DISAGREES_MAJOR_THRESHOLD_REL * declared, DISAGREES_MAJOR_THRESHOLD_MIN_MM)
    if abs(declared - measured) > tol:
        return (declared, measured)
    return None
