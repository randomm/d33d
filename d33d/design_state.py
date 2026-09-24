"""One serialised design-state block, shared by BOTH consumers (issue #120).

The largest failure mode is the user and the machine holding different
mental models of the object — the model has described a 30 mm sphere as 60
mm because the prompt carried no description of the existing design (the
bug this module pins). The fix: ONE serialised state block describing the
current object and its parameters, with provenance per value, built by ONE
function that both the live design prompt builder (``d33d.design_loop``)
and the API route the SPA reads (``d33d.versions_routes``) call — the same
callable, asserted by the tests, not two functions that happen to agree.

Entry shape (the contract the SPA's Brief renders and the prompt renders):
``name`` (the parameter key), ``kind`` (``"param" | "axis"`` — the
discriminator, below), ``label`` (the human label — for a parameter
with no label the label IS the parameter name, never invented prose),
``value`` (nullable), ``unit`` (``"mm"`` for numeric params, else ``None``),
and ``provenance`` — a ``Literal`` (closed set, never a bare ``str``;
the project's ``error_class`` enum is the precedent) of
``"stated" | "measured" | "assumed" | "unknown" | "disagrees"``, plus
``stated_value`` when ``provenance == "disagrees"``.

``kind`` (issue #246 review): ``"param"`` — a row built from the version's
params snapshot (the model emitted the value), ``"axis"`` — a row built
from the persisted per-axis stated set (the dimension protocol's W/D/H
axes). ``name`` is NOT unique within a block: a param row and an axis row
can both be named ``W`` (the model emits a ``W`` param AND the user stated
``W``) — ``kind``+``name`` is the row identity, and nothing in this module
dedupes, drops, or matches rows by name.

Provenance semantics (issue #246 — default to ``assumed``, promote to
``stated`` on evidence, NEVER the reverse):

- ``assumed`` — the model emitted this parameter; the user never said it.
  EVERY model-emitted parameter is ``assumed``: ``state_block_from_params``
  only ever sees the params snapshot, it never sees the user's words, so
  it cannot grant ``stated``. (Before #246 every non-zero param was
  labelled ``stated`` — the Brief told users they specified values the
  model invented.)
- ``stated`` — evidence from the dimension protocol: an axis (W/D/H) whose
  value the user actually said for the run that produced the version,
  carried on the version row as the persisted per-axis stated set
  (``versions.stated_dims``). ONLY the separate axis rows built from that
  set are ever ``stated``. No model-emitted parameter is ``stated`` in
  this ticket — not even one literally named W/D/H (DECISIONS.md: "key it
  on the protocol's confirmed set, not on parameter names"); the value
  the user said is never a license for a name match. The promotion seam
  (:func:`_maybe_promote_param`) is the single, clearly-marked place a
  later axis-binding ticket will extend to grant a model-emitted
  parameter evidence.
- ``measured`` / ``disagrees`` — a stated axis value compared against the
  persisted measurement (below).
- ``unknown`` — no value (never 0, never an omitted key).

``unknown`` is a REAL state and must survive serialisation as
``value: null`` — never ``0``, never an omitted key a consumer can
default. ``latest_version_stated_dims`` (``d33d.design_loop_events``)
returns ``None`` rather than a zero triple for exactly this reason; this
module follows it.

The block's bound: at most :data:`MAX_STATE_BLOCK_ENTRIES` entries
(12) reach the prompt — the prompt already carries the request, chat
history, reference dimensions and emission instruction, and the Brief's
own six-row display surface fits with headroom. Past the bound the
overflow is DROP-and-COUNT: the first 12 entries (declaration order —
JSON objects preserve insertion order) render, and a final
``… N more parameter(s)`` count line names the drop honestly (never
silently).

``measured`` / ``disagrees`` provenance is reachable from real data:
the versions table persists the measured bounding box of the render that
produced each version (issue #137 — the best candidate's bbox, the
matched component for multi-part models, NULL when no measurement was
obtainable). The shared callable :func:`state_block_for_version` compares
a STATED axis value (an axis row backed by persisted evidence) against
that measurement: within the tolerance the axis renders as ``measured``
(the displayed value is the MEASURED one — what will actually print),
outside it the axis renders as ``disagrees`` carrying BOTH numbers
(``stated_value`` rides alongside). An axis with no persisted stated
evidence and no measurement is OMITTED entirely — never rendered as a
fabricated value; the model's own W/D/H-named parameters still render as
``assumed`` param rows, so the number is never lost.

Rule (b) promotion (issue #250 — explicit user confirmation): a param
row is ALSO ``stated`` when its version's persisted ``confirmed_params``
set (the param-keyed ``{name: value}`` written ONLY by the accepted-
offer flow — never by name inference) carries the param with a value
equal (within ``CONFIRMED_VALUE_TOLERANCE`` = 1e-6) to the param's
current value. The operator decision: a param becomes ``stated`` when
EITHER (a) the #248 axis evidence holds OR (b) explicit confirmation
holds — for ANY param (the design team's own example offer is a wall
thickness, which has no axis). A confirmed value that the param no
longer carries (a value change in this version) does NOT promote — the
confirmation is stale evidence. The confirmed set is INDEPENDENT of the
axis-keyed ``stated_dims`` store: confirming a wall thickness does not
close the W/D/H axis evidence, and vice versa.

The prompt rendering marks provenance (``format_design_state_block`` /
``format_design_state_line``): an ``assumed`` value renders
``label = value (assumed — the user never set this)`` and a ``stated``
value renders ``label = value (stated by the user)`` — the model must be
able to tell user-set values from its own guesses.
"""

from __future__ import annotations

from typing import Any, Literal, NotRequired, TypedDict

#: The row-kind discriminator: ``"param"`` (a row built from the version's
#: params snapshot — the model emitted the value) vs ``"axis"`` (a row
#: built from the persisted per-axis stated set — the dimension
#: protocol's W/D/H axes). ``name`` alone is NOT the row identity — a
#: param row and an axis row can share the name ``W``.
RowKind = Literal["param", "axis"]

#: The dimension protocol's axis words, used to render an axis row's
#: prompt line (``Width (W) = 60 (stated by the user)``) so the model can
#: tell an axis row from its own ``W`` parameter. Defined ONCE here for
#: the prompt; the SPA carries the same words in ``copy.ts``
#: (``copy.brief.axisLabel``) for its own display.
AXIS_LABELS: dict[str, str] = {"W": "Width", "D": "Depth", "H": "Height"}

from d33d.design_loop import (
    BBOX_TOLERANCE_MIN_MM,
    BBOX_TOLERANCE_REL,
)

__all__ = [
    "AXIS_PARAM_NAMES",
    "MAX_STATE_BLOCK_ENTRIES",
    "Provenance",
    "StateEntry",
    "build_design_state_block",
    "format_design_state_block",
    "normalize_param_meta",
    "persisted_bbox_extents",
    "state_block_for_version",
    "state_block_from_params",
]

#: The bound on how many entries reach the prompt (and the SPA's Brief).
#: The prompt already carries the request, chat history, reference
#: dimensions, and emission instruction; 12 entries keeps the block a
#: single, readable, size-bounded section (the Brief's six-row display
#: surface fits with headroom). Past the bound the overflow is
#: drop-and-count (see :func:`build_design_state_block`).
MAX_STATE_BLOCK_ENTRIES = 12

#: The closed provenance set (the project's ``error_class`` enum is the
#: precedent — a ``Literal``, never a bare ``str``). ``stated`` (the user
#: said this value — evidence from the dimension protocol, persisted on
#: the version row), ``measured`` (a stated value a render confirmed,
#: within tolerance), ``assumed`` (the model emitted this value — the
#: user never set it; issue #246's default for every model-emitted
#: parameter), ``unknown`` (no value — value serialises to ``null``),
#: ``disagrees`` (a measured value exists that differs from the stated
#: one — both numbers are carried).
Provenance = Literal["stated", "measured", "assumed", "unknown", "disagrees"]


class StateEntry(TypedDict):
    """One design-state entry (the serialised contract).

    ``value`` is nullable: ``"unknown"`` provenance carries ``None``.
    ``stated_value`` is present ONLY when ``provenance == "disagrees"``
    (the displayed value is the MEASURED one — what will print — and the
    stated value rides alongside; they are never collapsed), so it is
    ``NotRequired``: the common case carries no ``stated_value`` key at
    all.
    """

    name: str
    kind: RowKind
    label: str
    value: float | str | bool | None
    unit: str | None
    provenance: Provenance
    stated_value: NotRequired[float | str | bool | None]
    #: ``True`` when the label is the raw SCAD identifier (no model label)
    #: — the UI renders it in the mono face (mono = machine value).
    label_is_identifier: NotRequired[bool]
    #: The model's declared axis (``"W" | "D" | "H"``) — the ONLY
    #: promotion evidence (issue #248). Present on param rows only.
    axis: NotRequired[str]
    #: The model's stated reason for a value the user did not give
    #: (issue #248) — the Brief's expanded assumed row renders it.
    reason: NotRequired[str]


def _is_number(value: Any) -> bool:
    """True for int/float (bool is excluded — it is not a measurement)."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _entry(
    name: str,
    value: Any,
    provenance: Provenance,
    stated_value: Any = None,
    kind: RowKind = "param",
) -> dict[str, Any]:
    """One entry from a (name, value) pair (provenance supplied)."""
    out: dict[str, Any] = {
        "name": name,
        "kind": kind,
        "label": name,  # label IS the parameter name (no invented prose)
        "label_is_identifier": True,  # no model label — raw identifier
        "value": value,
        "unit": "mm" if _is_number(value) else None,
        "provenance": provenance,
    }
    if provenance == "disagrees":
        out["stated_value"] = stated_value
    return out


#: The axes a model may declare in a parameter's metadata (issue #248).
_VALID_META_AXES: frozenset[str] = frozenset(("W", "D", "H"))


def normalize_param_meta(raw: Any) -> dict[str, Any]:
    """The model's ``parameters`` metadata, normalised to
    ``{name: {label?, unit?, axis?, reason?}}`` (issue #248).

    ``raw`` is the version row's persisted ``param_meta`` column (``None``
    — legacy rows, or a model that emitted no ``parameters`` array — or
    the JSON the write path stored). ``None`` / an absent / malformed
    input degrades to ``{}`` ("no metadata" — every entry falls back to
    its identifier label), NEVER a crash: a metadata failure must never
    fail the design state (or the design pass).

    Each stored value keeps only the fields that survive a shape check:
    ``label``/``unit``/``reason`` as non-empty strings, ``axis`` only when
    one of ``W``/``D``/``H``. A metadata entry whose name is not a
    non-empty string is dropped (the join is by name — a nameless entry
    has nothing to join against), as is one that carries no usable
    field (storing it would be a no-op the caller could not distinguish
    from absence). The result is insertion-ordered (declaration order).
    """
    if not isinstance(raw, dict):
        return {}
    out: dict[str, Any] = {}
    for name, meta in raw.items():
        if not isinstance(name, str) or not name:
            continue
        if not isinstance(meta, dict):
            continue
        cleaned: dict[str, Any] = {}
        for key in ("label", "unit", "axis", "reason"):
            v = meta.get(key)
            if not isinstance(v, str) or not v:
                continue
            if key == "axis" and v not in _VALID_META_AXES:
                continue
            cleaned[key] = v
        if cleaned:
            out[name] = cleaned
    return out


def state_block_from_params(
    params: dict[str, Any] | None,
    param_meta: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """The design-state block from a version's params snapshot
    (``versions_service.latest_version(project_id)["params"]``) with
    the model's per-parameter metadata joined in (issue #248).

    This is the LIVE route's data source. Every declared parameter
    (never a fixed {W, D, H} triple) becomes an entry:

    - a known numeric/bool value → provenance ``"assumed"`` (issue #246:
      the model emitted the value; the user's words are never visible
      here, so no parameter — axis-named or not, numeric or not — is
      ever ``"stated"`` from a params snapshot alone), ``unit`` ``"mm"``
      for numeric, ``None`` for non-numeric (a string/bool parameter has
      no mm meaning);
    - a missing/null/zero/absent value → provenance ``"unknown"`` with
      ``value: None`` (``null`` — never ``0``, never an omitted key).

    ``param_meta`` (issue #248) is the version row's persisted
    ``param_meta`` (``{name: {label?, unit?, axis?, reason?}}`` — the
    model's own words about each parameter it declared). The join is BY
    NAME: an entry for a declared name takes that name's ``label``
    (``label_is_identifier`` falls away) and, when the entry has no
    unit of its own, the metadata's ``unit``; a metadata name that no
    param declares is IGNORED (never surfaced as an entry); a param
    with no metadata entry keeps ``label == name`` +
    ``label_is_identifier: True`` (the raw identifier, rendered mono by
    the UI). No label is ever synthesised (no prettifying — DECISIONS.md).

    ``None`` (no version yet) → an empty block (zero entries).
    """
    if params is None:
        return []
    meta = normalize_param_meta(param_meta)
    out: list[dict[str, Any]] = []
    for name, value in params.items():
        if _is_number(value):
            if value == 0:
                entry = _entry(name, None, "unknown")
            else:
                entry = _entry(name, value, "assumed")
        elif value is None:
            entry = _entry(name, None, "unknown")
        else:
            # string/bool — a real parameter, model-emitted, no mm unit.
            entry = _entry(name, value, "assumed")
        m = meta.get(name)
        if m is not None:
            label = m.get("label")
            if isinstance(label, str) and label:
                entry["label"] = label
                entry["label_is_identifier"] = False
            if entry["unit"] is None:
                unit = m.get("unit")
                if isinstance(unit, str) and unit:
                    entry["unit"] = unit
            axis = m.get("axis")
            if isinstance(axis, str) and axis in _VALID_META_AXES:
                entry["axis"] = axis
            reason = m.get("reason")
            if isinstance(reason, str) and reason:
                entry["reason"] = reason
        out.append(entry)
    return out


#: The parameter names that map onto the bbox's axes (``W`` → x,
#: ``D`` → y, ``H`` → z — the ``_dim_params`` convention ``d33d.design_loop``
#: threads to the render as ``defines``). An axis name is the ONLY
#: evidence a parameter can carry in this module: a parameter with no
#: axis name (``rod_bore``, ``wall_thickness``) is never compared against
#: a measurement and never promoted — it stays ``assumed`` (or
#: ``unknown``), never silently becomes ``stated``/``measured``.
AXIS_PARAM_NAMES: tuple[str, str, str] = ("W", "D", "H")


def persisted_bbox_extents(measurement: Any) -> tuple[float, float, float] | None:
    """The per-axis extents to compare stated values against, or ``None``
    (issue #137).

    ``measurement`` is the version row's persisted ``bbox`` (``None`` —
    no measurement was obtainable at version creation — or the JSON
    ``{"x": ..., "y": ..., "z": ...}`` the write path stored). ``None``
    → ``None`` (ABSTAIN: a persisted NULL means "no measurement", never
    a fabricated ``(0.0, 0.0, 0.0)`` — issue #91's precedent). A stored
    zero axis also abstains: a zero extent is not a measurement, it is
    the encoded absence, and comparing a stated value against ``0`` would
    mark every axis ``disagrees`` for a version that never measured.
    """
    if measurement is None:
        return None
    if not isinstance(measurement, dict):
        return None
    axes = [measurement.get(name) for name in ("x", "y", "z")]
    if not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in axes):
        return None
    extents = tuple(float(v) for v in axes)  # type: ignore[arg-type]
    if any(e <= 0 for e in extents):
        return None
    return extents


def _axis_row(
    axis: str,
    value: float,
    provenance: Provenance,
    stated_value: Any = None,
) -> dict[str, Any]:
    """An axis row (name = W/D/H) for the design-state block."""
    return _entry(axis, value, provenance, stated_value, kind="axis")


def _axis_stated_evidence(
    stated: dict[str, Any] | None,
) -> dict[str, float]:
    """The per-axis stated set as positive floats, or ``{}``.

    ``stated`` is the version row's persisted ``stated_dims`` (the
    dimension protocol's per-axis confirmed set for the run that produced
    the version — ``{"W": 60.0, "H": 80.0}`` for a partial statement,
    ``None`` when the user stated nothing). A zero/missing/non-numeric
    axis is dropped (issue #91's abstain semantics: a zero is the encoded
    absence, never a target).
    """
    if not stated:
        return {}
    out: dict[str, float] = {}
    for axis in AXIS_PARAM_NAMES:
        v = stated.get(axis)
        if _is_number(v) and v > 0:
            out[axis] = float(v)
    return out


def state_block_for_version(
    params: dict[str, Any] | None,
    measurement: Any = None,
    stated: dict[str, Any] | None = None,
    param_meta: dict[str, Any] | None = None,
    confirmed_params: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """The design-state block for ONE version: the version's params
    snapshot (``state_block_from_params`` — every model-emitted parameter
    ``assumed``/``unknown``, labels from the model's own metadata) merged
    with the version's PERSISTED per-axis stated set and PERSISTED
    measurement (issue #246 + #137).

    This is THE shared callable — the live prompt builder
    (``d33d.design_loop``) and the GET route the SPA reads
    (``d33d.versions_routes``) both call it (identity pinned by the
    tests, not output equality).

    ``measurement`` is the version row's ``bbox`` field as the write path
    stored it (issue #137: the best candidate's render bbox — the MATCHED
    COMPONENT for a multi-part model, ``None`` when the component was not
    identifiable or no measurement was obtained). ``stated`` is the
    version row's persisted ``stated_dims`` (issue #246: the dimension
    protocol's per-axis confirmed set for the run that produced the
    version — a partial statement counts for the axes it states, ``None``
    when nothing was stated). ``param_meta`` is the version row's
    persisted ``param_meta`` (issue #248: the model's per-parameter
    ``{name: {label?, unit?, axis?, reason?}}`` — ``None`` on every
    legacy row, which degrades to the identifier fallback for all
    entries). All ``None`` → the pure params-only substrate's output,
    verbatim.

    Axis rows are driven by the PERSISTED per-axis stated set, never by
    param names: for each axis in ``stated`` the block carries a W/D/H
    row, and that row's provenance comes from the measurement:

    - no measurement (or no persisted bbox) → ``stated`` (the user said
      it, no render yet to confirm it);
    - the measured axis is within the named tolerance (``max(
      BBOX_TOLERANCE_REL * stated, BBOX_TOLERANCE_MIN_MM)`` — the same
      rule the bbox gate applies, ``d33d.design_loop``'s constants) →
      ``measured``, and the DISPLAYED value is the measured one (what
      will actually print), not the stated one;
    - outside the tolerance → ``disagrees`` carrying BOTH numbers:
      ``value`` is the measured one (the display), ``stated_value`` the
      stated one (the ride-along).

    An axis with NO persisted stated value and NO measurement is OMITTED
    entirely — never rendered as a fabricated value (a bare
    ``stated = {}`` yields no axis rows at all; the model's own W/D/H-
    named parameters still appear as ``assumed`` param rows, so the
    number is never lost). An axis with a persisted stated value but no
    measurement keeps its row as ``stated``.

    No model-emitted parameter is promoted to ``stated`` without its
    metadata declaring an axis (issue #248 fills the seam below — see
    :func:`_maybe_promote_params`): the model's ``axis`` field is the
    ONLY promotion evidence; a param named W/D/H without one is never
    promoted (DECISIONS.md: "key it on the protocol's confirmed set, not
    on parameter names").

    The MEASUREMENT comparison (issue #137) still applies to a param
    literally named W/D/H that the snapshot carries — regardless of the
    persisted stated set (within the named tolerance the param row renders
    ``measured`` with the measured value displayed; outside it ``disagrees``
    with the stated value riding along). The persisted stated set only
    drives the SEPARATE axis rows (appended after the param rows); the
    two surfaces are independent and never name-matched.

    ``confirmed_params`` (issue #250, rule (b)) is the version row's
    persisted param-keyed confirmed set (``{name: value}`` — written only
    by the accepted-offer flow). A param in the set with a value equal
    to its current value (within 1e-6) renders ``stated`` — for ANY
    param, axis or not. The confirmation is FROZEN evidence of that
    value: a W/D/H-named param that ALSO carries a measurement keeps the
    full comparison (within tolerance → ``measured``; outside it →
    ``disagrees`` with the confirmed value riding along as
    ``stated_value`` — the measurement is the number that will print,
    and a confirmed value the measurement contradicts by an unbounded
    margin must not silently win). A param WITHOUT a measurement (or not
    W/D/H-named) skips nothing: there is no comparison to re-open, and
    the promoted row renders ``stated``. ``None`` / empty → no rule (b)
    promotion.
    """
    from d33d.confirm_offer import CONFIRMED_VALUE_TOLERANCE

    entries = state_block_from_params(params, param_meta)
    evidence = _axis_stated_evidence(stated)
    # The single, clearly-marked promotion seam (issue #248, filled):
    # metadata-declared axis + persisted confirmed value + within-tolerance
    # match promotes ``assumed`` → ``stated``. Name-based promotion is
    # still forbidden — only a declared ``axis`` field qualifies.
    entries = _maybe_promote_params(entries, evidence)
    # Rule (b) (issue #250): explicit user confirmation promotes a param
    # ``assumed`` → ``stated`` when the confirmed set carries its current
    # value. The promotion only — the W/D/H-named param below still runs
    # the FULL measurement comparison (a confirmed number the measurement
    # contradicts renders ``disagrees``; the confirmation is frozen
    # evidence of a value, never a measurement bypass).
    _maybe_confirm_params(entries, confirmed_params, CONFIRMED_VALUE_TOLERANCE)
    extents = persisted_bbox_extents(measurement)
    if not evidence and extents is None:
        # No stated evidence and no measurement: the params-only
        # substrate, verbatim (no axis rows — an axis with no stated
        # value and no measurement is omitted entirely).
        return entries

    out: list[dict[str, Any]] = []
    for entry in entries:
        # The measurement comparison applies to the snapshot's own
        # W/D/H-named params (issue #137 — the stated value the version
        # was built with is the param's own value when the user stated
        # nothing per axis; the model's number is the best available
        # reference and is what the #137 gate compares against). A rule
        # (b) confirmed param runs it too — the comparison is the
        # measurement's own honesty, independent of who stated the
        # value: within tolerance the confirmed value holds as
        # ``measured``; outside it the row renders ``disagrees`` with
        # the stated (confirmed) value riding along.
        name = entry["name"]
        if extents is not None and name in AXIS_PARAM_NAMES:
            stated_value = entry["value"]
            if _is_number(stated_value) and stated_value > 0:
                extent = extents[AXIS_PARAM_NAMES.index(name)]
                tol = max(BBOX_TOLERANCE_REL * stated_value, BBOX_TOLERANCE_MIN_MM)
                e = dict(entry)
                if abs(extent - stated_value) <= tol:
                    e["value"] = extent
                    e["provenance"] = "measured"
                else:
                    e["value"] = extent
                    e["provenance"] = "disagrees"
                    e["stated_value"] = stated_value
                out.append(e)
                continue
        out.append(entry)

    # Axis rows from the persisted per-axis stated set (declaration order
    # W/D/H — the protocol's axis order), appended AFTER the param rows.
    # A param row and an axis row for the same letter can coexist (e.g.
    # the model emits an ``H`` param and the user stated ``H`` too): the
    # param row keeps its ``assumed`` value, the axis row carries the
    # user's stated evidence — both render, never collapsed, never
    # guessed, never name-matched.
    for axis in AXIS_PARAM_NAMES:
        if axis not in evidence:
            continue
        axis_value = evidence[axis]
        if extents is None:
            out.append(_axis_row(axis, axis_value, "stated"))
            continue
        extent = extents[AXIS_PARAM_NAMES.index(axis)]
        tol = max(BBOX_TOLERANCE_REL * axis_value, BBOX_TOLERANCE_MIN_MM)
        if abs(extent - axis_value) <= tol:
            out.append(_axis_row(axis, extent, "measured"))
        else:
            out.append(_axis_row(axis, extent, "disagrees", axis_value))
    return out


def _maybe_confirm_params(
    entries: list[dict[str, Any]],
    confirmed: dict[str, Any] | None,
    tolerance: float = 1e-6,
) -> None:
    """Rule (b) promotion (issue #250, filled): explicit user
    confirmation.

    A param row is promoted ``assumed`` → ``stated`` iff the version's
    persisted ``confirmed_params`` set (``{name: value}`` — written ONLY
    by the accepted-offer flow in ``d33d.projects.post_chat``; never by
    name inference) carries the param with a value equal to the param's
    CURRENT value within ``tolerance`` (1e-6, issue #250's operator
    decision). The promotion applies to ANY param — axis or not (the
    design team's example offer is a wall thickness, which has no axis),
    numeric or not (non-numeric values compare with ``==``, no tolerance
    arithmetic). A confirmed value the param no longer carries does NOT
    promote (stale evidence — the value changed in this version). The
    set is independent of the axis-keyed ``stated_dims`` store: a
    confirmation never touches the W/D/H axis rows.

    The promotion does NOT exempt the param from the measurement
    comparison: a W/D/H-named confirmed param keeps it (within tolerance
    → ``measured``; outside → ``disagrees`` with the confirmed value
    riding along) — the measurement is the number that will print, and a
    confirmed value the measurement contradicts by an unbounded margin
    must not silently win.

    Do not add confirmation promotion anywhere else; do not infer a
    confirmation from a parameter name.
    """
    if not confirmed:
        return
    for entry in entries:
        if entry.get("kind") != "param":
            continue
        if entry["provenance"] != "assumed":
            continue  # unknown / already stated/measured: nothing to confirm
        if entry["name"] not in confirmed:
            continue
        confirmed_value = confirmed[entry["name"]]
        value = entry.get("value")
        if isinstance(confirmed_value, (int, float)) and not isinstance(
            confirmed_value, bool
        ):
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                continue
            if abs(float(value) - float(confirmed_value)) > tolerance:
                continue
        elif confirmed_value != value:
            continue
        entry["provenance"] = "stated"


def _maybe_promote_params(
    entries: list[dict[str, Any]],
    stated_evidence: dict[str, float],
) -> list[dict[str, Any]]:
    """The single, clearly-marked promotion seam (issue #248, filled).

    A model-emitted parameter is promoted ``assumed`` → ``stated`` iff
    THREE conditions hold — the model's metadata declares an axis for it
    (``entry["axis"]``, set ONLY from the model's ``parameters`` metadata
    during the join in :func:`state_block_from_params` — a param named
    ``W``/``D``/``H`` WITHOUT a declared axis has no ``axis`` key and is
    not promoted, never is), the version's persisted confirmed set
    contains that axis, and the param's value is numeric, positive, and
    within the existing bbox tolerance of the CONFIRMED value for that
    axis (``max(BBOX_TOLERANCE_REL * confirmed, BBOX_TOLERANCE_MIN_MM)``
    — the same rule the bbox gate applies). A promotion only ever lowers
    the burden of proof, never changes the displayed value (the param
    keeps its own number), and never runs on a non-numeric value (no
    tolerance arithmetic on strings/bools — they stay ``assumed``).

    The comparison target is the CONFIRMED value (the user's evidence,
    ``stated_dims``) — never the measured bbox. Do not add promotion
    anywhere else; do not infer an axis from a parameter name.
    """
    if not stated_evidence:
        return entries
    for entry in entries:
        axis = entry.get("axis")
        if entry.get("kind") != "param" or axis not in _VALID_META_AXES:
            continue
        if entry["provenance"] != "assumed":
            continue
        confirmed = stated_evidence.get(axis)
        if confirmed is None:
            continue
        value = entry.get("value")
        if not _is_number(value) or value <= 0:
            continue  # non-numeric / zero: never promoted, no arithmetic
        tol = max(BBOX_TOLERANCE_REL * confirmed, BBOX_TOLERANCE_MIN_MM)
        if abs(value - confirmed) <= tol:
            entry["provenance"] = "stated"
    return entries


def build_design_state_block(
    entries: list[dict[str, Any]],
    max_entries: int = MAX_STATE_BLOCK_ENTRIES,
) -> dict[str, Any]:
    """The serialised block: entries bounded to ``max_entries`` with a
    drop-and-count overflow marker.

    The bound is enforced HERE (the single function both consumers call):
    at most ``max_entries`` entries reach the prompt (or the SPA). Past
    the bound the overflow is dropped and counted — the first
    ``max_entries`` entries (declaration order) render and the block's
    ``dropped_count`` carries how many were omitted. ``dropped_count == 0``
    means nothing was dropped (the count line is omitted by the formatter).
    """
    dropped = max(0, len(entries) - max_entries)
    return {
        "entries": list(entries[:max_entries]),
        "dropped_count": dropped,
        "unit": "mm",
    }


def _provenance_suffix(provenance: Any) -> str:
    """The prompt's provenance mark (issue #246): the model must be able
    to tell user-set values from its own guesses. ``stated`` and
    ``assumed`` carry a mark; ``measured``/``disagrees``/``unknown`` keep
    their existing rendering."""
    if provenance == "stated":
        return " (stated by the user)"
    if provenance == "assumed":
        return " (assumed — the user never set this)"
    return ""


def _axis_prefix(entry: dict[str, Any]) -> str:
    """The line prefix for one entry: an axis row (``kind == "axis"``)
    renders its axis word first — ``Width (W)`` — so the model can tell
    the protocol's axis row from its own ``W`` parameter; a param row
    keeps its bare name."""
    if entry.get("kind") == "axis":
        name = str(entry.get("name") or "")
        word = AXIS_LABELS.get(name, name)
        return f"{word} ({name})"
    label = entry.get("label")
    return label if label else (entry.get("name") or "")


def _render_value_line(name: str, value: Any, provenance: Any) -> str:
    """One entry as ``label = value`` + the provenance mark (if any)."""
    if _is_number(value):
        value_str = f"{value:g}"
    elif value is None:
        value_str = "not specified"
    else:
        value_str = str(value)
    return f"{name} = {value_str}{_provenance_suffix(provenance)}"


def _render_value_inline(name: str, value: Any, provenance: Any) -> str:
    """One entry as ``label=value`` + the provenance mark (if any)."""
    if _is_number(value):
        value_str = f"{value:g}"
    elif value is None:
        value_str = "not specified"
    else:
        value_str = str(value)
    return f"{name}={value_str}{_provenance_suffix(provenance)}"


def format_design_state_block(block: dict[str, Any]) -> str:
    """The block as prompt text (the live prompt's rendering of the block).

    One line per entry (``label = value`` — ``not specified`` for a
    ``null`` value, plus the provenance mark: ``stated`` renders
    ``(stated by the user)``, ``assumed`` renders ``(assumed — the user
    never set this)``), then a final ``… N more parameter(s)`` count line
    when ``dropped_count > 0``. An empty block (no version yet) renders a
    single ``not specified`` line so the section is never silently absent
    (the 30/60 bug is a prompt that carries no description of the
    existing design — an absent block is the bug itself).
    """
    lines: list[str] = []
    for entry in block.get("entries", []):  # type: ignore[union-attr]
        lines.append(
            _render_value_line(
                _axis_prefix(entry), entry.get("value"), entry.get("provenance")
            )
        )
    if block.get("dropped_count", 0) > 0:
        n = block["dropped_count"]
        lines.append(f"… {n} more parameter{'s' if n != 1 else ''}")
    if not lines:
        lines.append("(no parameters yet)")
    return "Current design state (mm):\n" + "\n".join(lines)


def format_design_state_line(entries: list[dict[str, Any]]) -> str:
    """A single-line variant of the block (no header, no count line) —
    the prompt's ground-truth line. Empty block → ``not specified``.
    (The live prompt's system line uses the compact W/D/H triple; this
    helper is provided for any consumer that wants the full entry set on
    one line.)"""
    if not entries:
        return "not specified"
    parts: list[str] = []
    for entry in entries:
        parts.append(
            _render_value_inline(
                _axis_prefix(entry), entry.get("value"), entry.get("provenance")
            )
        )
    return ", ".join(parts)
