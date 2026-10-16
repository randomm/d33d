"""The stated-feature matcher (issue #433, module split issue #447).

A user who says ``a 5 mm hole`` has stated a FEATURE size, not the part's
envelope. This module answers the two questions the design-state block
asks about those statements:

1. **Match + promote** (:func:`apply_stated_features`) — which of the
   model's parameters does this stated feature describe, and did the model
   actually make that size? Within ``FEATURE_MISMATCH_TOLERANCE_MM`` the
   row is promoted ``stated`` (the user did say the value); strictly
   beyond it the row renders ``disagrees`` with the stated value riding
   along (the Brief's ochre ``stated 5 / made 8`` row).
2. **Report** (:func:`feature_mismatches_from_block`) — the mismatches the
   block carries, as the done-frame payload. The reply's list is built
   from THIS function, so the Brief row and the reply are one fact by
   construction — never a second comparison that can drift from the row.

The role vocabulary is NOT defined here. ``FEATURE_ROLE_TOKENS`` /
``SIZE_PARAM_TOKENS`` live in ``d33d.axis_lexicon`` (issue #433) so the
extraction's role words and the matcher's bindings derive from one map and
cannot drift apart.

Leaf of the design-state import graph: imports ``d33d.axis_lexicon`` and
``d33d.param_tokens`` only. It must never import ``d33d.design_state``
(which imports this module at top level) and the matcher's public names
must not gain a lazy import to dodge that — the primitive both sides need
lives in ``param_tokens`` for exactly this reason (issue #447).
``StatedFeature`` is imported under ``TYPE_CHECKING`` (annotations are
strings under ``from __future__ import annotations``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from d33d.axis_lexicon import FEATURE_ROLE_TOKENS, SIZE_PARAM_TOKENS
from d33d.param_tokens import is_number, split_name_tokens

if TYPE_CHECKING:
    from d33d.statement_extraction import StatedFeature

__all__ = [
    "FEATURE_MISMATCH_TOLERANCE_MM",
    "apply_stated_features",
    "feature_mismatches_from_block",
    "match_stated_features",
    "param_tokens",
]

#: The feature-role tolerance (issue #433): a stated feature value
#: matches the model's param value when ``|stated − made| <=
#: FEATURE_MISMATCH_TOLERANCE_MM`` (a diff of EXACTLY 0.05 is NOT a
#: mismatch — the operator decision; the flag fires only strictly above
#: it). Deliberately tighter than the bbox tolerance (``max(1%, 0.5 mm)``
#: in ``d33d.design_loop``) — a feature size the user stated to the
#: millimetre is not the part's envelope, and a 1% / 0.5 mm slack would
#: let a 5 mm hole through as 8 mm without a flag. The constant is
#: independent of the bbox pair for the same reason ``DISAGREES_MAJOR_*``
#: is — different magnitudes, different questions, never unified.
FEATURE_MISMATCH_TOLERANCE_MM: float = 0.05

#: The size param tokens and the closed role vocabulary are defined ONCE in
#: ``d.axis_lexicon`` (issue #433) — the extraction's role words derive from
#: the same map, so the matcher and the extraction cannot drift apart.
_SIZE_TOKENS: frozenset[str] = SIZE_PARAM_TOKENS
_FEATURE_ROLE_TOKENS: dict[str, frozenset[str]] = FEATURE_ROLE_TOKENS

#: Dimension tokens that name a NON-size role: a role-less feature size never
#: binds a param carrying one of these (``a 5 mm hole`` must not flag
#: ``hole_depth``).
_NON_SIZE_DIM_TOKENS: frozenset[str] = frozenset({"depth", "width", "length", "height"})


def param_tokens(name: str, label: str | None) -> set[str]:
    """The param's token set: the name's :func:`split_name_tokens` plus
    the label's whitespace/underscore tokens (lowercased, punctuation
    stripped) — the matcher's token source (issue #433)."""
    tokens = set(split_name_tokens(name))
    if label:
        for raw in label.replace("_", " ").split():
            cleaned = raw.strip(".,;:!?()[]{}\"'").lower()
            if cleaned:
                tokens.add(cleaned)
    return tokens


def match_stated_features(
    entries: list[dict[str, Any]],
    param_meta: dict[str, Any] | None,
    stated_features: list[StatedFeature] | None,
) -> dict[str, StatedFeature]:
    """The param-keyed stated feature values (issue #433).

    Matches each stated feature to the param(s) its noun + role name via
    :func:`param_tokens` (the name + label token set): the param's
    tokens must carry the feature noun AND (when the record has an
    explicit role) a token in the role's closed set (the noun guard — a
    bare ``d`` without the noun never matches). Zero matches → the
    param gets no value (the stated value stays in state, unflagged —
    the operator decision); multiple matches → every matching param
    gets the value (the operator decision — flag all). Returns
    ``{param_name: stated_value}`` for the params that matched; an empty
    dict when nothing matched (the caller keeps the params as-is).
    Non-numeric / zero param values are never matched (the comparison
    needs a positive made value). Returns ``{param_name: StatedFeature}``
    — the matched record itself, so the caller reads its noun and value.
    """
    if not stated_features:
        return {}
    meta = param_meta if isinstance(param_meta, dict) else {}
    out: dict[str, StatedFeature] = {}
    ambiguous: set[str] = set()
    for f in stated_features:
        value = f.value
        if not is_number(value):
            continue
        if float(value) <= 0:
            continue
        noun = f.noun
        role = f.role
        if role is not None and role not in _FEATURE_ROLE_TOKENS:
            continue  # an unknown role binds nothing (never a guess)
        role_tokens = _FEATURE_ROLE_TOKENS.get(role) if role else None
        for entry in entries:
            if entry.get("kind") != "param":
                continue
            value_ = entry.get("value")
            if not is_number(value_):
                continue
            if float(value_) <= 0:
                continue
            name = entry.get("name")
            label = None
            m = meta.get(name)
            if isinstance(m, dict):
                label = m.get("label")
            tokens = param_tokens(name, label if isinstance(label, str) else None)
            if noun not in tokens:
                continue  # the noun guard: no feature noun → no match
            if role is None:
                # Role-less: a size param (diameter/size token) or the bare
                # noun param — never a depth/width/length/height param.
                if tokens & _NON_SIZE_DIM_TOKENS:
                    continue
                if not (tokens & _SIZE_TOKENS or tokens <= {noun}):
                    continue
            elif not (tokens & role_tokens):
                continue  # the role must also be present
            prior = out.get(name)
            if prior is not None and prior.value != f.value:
                ambiguous.add(name)  # two stated values, one param: no call
            out.setdefault(name, f)
    for name in ambiguous:
        out.pop(name, None)
    return out


def apply_stated_features(
    entries: list[dict[str, Any]],
    param_meta: dict[str, Any] | None,
    stated_features: list[StatedFeature] | None,
) -> None:
    """The stated-feature promotion + mismatch flag (issue #433).

    In-place on ``entries`` (the param rows). A param the user's stated
    feature matches (``match_stated_features`` — noun + role tokens)
    and whose row is ``assumed`` (the model picked the value; the user
    never confirmed it via the offer flow) is promoted ``stated`` when
    the made value is within ``FEATURE_MISMATCH_TOLERANCE_MM`` of the
    stated one (the ``a 5 mm hole`` / ``hole_diameter = 5`` case —
    honest provenance, no flag), and rendered ``disagrees`` (``value``
    the made value, ``stated_value`` the stated one, source ``"user"`` —
    the user gave the evidence) when the made value is STRICTLY more
    than the tolerance away (the ``a 5 mm hole`` / ``hole_diameter = 8``
    case — the Brief's ochre ``stated 5 / made 8`` row). A param already
    ``stated`` (the offer confirmation, or an axis promotion) or
    ``measured`` is never touched — the feature comparison is the FIRST
    consumer of a still-``assumed`` feature param, and a param is never
    compared twice. A param with no stated-feature match is never
    touched (zero-match → no flag, the stated value stays in state).
    """
    if not stated_features:
        return
    matched = match_stated_features(entries, param_meta, stated_features)
    if not matched:
        return
    for entry in entries:
        if entry.get("kind") != "param":
            continue
        if entry.get("provenance") != "assumed":
            continue  # only the model's own (unconfirmed) value is compared
        name = entry.get("name")
        if name not in matched:
            continue
        value = entry.get("value")
        if not is_number(value) or value <= 0:
            continue
        feature = matched[name]
        stated_value = float(feature.value)
        entry["stated_feature"] = feature.noun
        # Round the diff: float subtraction lands just above an exact 0.05
        # (1.05 - 1.0 == 0.050000000000000044); the tolerance is exact.
        if round(abs(float(value) - stated_value), 6) <= FEATURE_MISMATCH_TOLERANCE_MM:
            entry["provenance"] = "stated"
        else:
            entry["provenance"] = "disagrees"
            entry["stated_value"] = stated_value
            entry["disagrees_source"] = "user"


def feature_mismatches_from_block(
    entries: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """The stated-vs-made feature mismatches the design-state block carries
    (issue #433): one record per ``disagrees`` row whose stated value came
    from the user's feature statement (``stated_feature`` + ``stated_value``).
    The reply's done-frame list is built from THIS function, so the Brief row
    and the reply are one fact by construction (never a second comparison).
    """
    out: list[dict[str, Any]] = []
    for e in entries:
        if e.get("kind") != "param" or e.get("provenance") != "disagrees":
            continue
        if e.get("disagrees_source") != "user" or "stated_feature" not in e:
            continue
        made = e.get("value")
        if not is_number(made):
            continue
        label = e.get("label")
        out.append(
            {
                "noun": e["stated_feature"],
                "stated_mm": float(e["stated_value"]),
                "made_mm": float(made),
                "label": label if isinstance(label, str) and label else e["name"],
            }
        )
    return out
