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
``stated_value`` when ``provenance == "disagrees"`` (a collapsed row may
    also carry the param's value alongside its ``stated`` provenance —
    the two surfaces are independent and the stated value rides the
    axis row), and
``disagrees_source`` (``"model" | "user"``) on PARAM rows whose
provenance is ``"disagrees"`` — ``"user"`` when the stated value is the
user's (the default; the field is ABSENT on the wire for backward
compat in that case), ``"model"`` when an ASSUMED param with a declared
``axis`` (issue #248 metadata) differs from that axis's measured extent
beyond the bbox tolerance (issue #264 — the Brief renders the model
copy "I set X, it measures Y", never "You asked for"). Axis rows NEVER
carry the field — an axis-row disagreement is always user-stated.

What is compared (issue #264 — measurement honesty): every PARAM row
whose provenance is ``stated`` with a positive numeric value is compared
against its reference extent with the bbox tolerance (``max(
BBOX_TOLERANCE_REL * stated, BBOX_TOLERANCE_MIN_MM)``, the #137 rule):
a param literally named W/D/H against that letter's measured extent
(the #137 path, unchanged), and an axis-DECLARED param (param_meta
``axis``) against that axis's measured extent — including one promoted
to ``stated`` (rule (a) axis match or rule (b) confirmed_params). Inside
tolerance the row is ``measured`` (the measured extent displayed);
outside it ``disagrees`` with the param's own value as ``stated_value``
and source ``"user"`` (the user gave the evidence — the model's assumed
number is the ride-along only via its own model-source path). A param is
never compared twice; each param row's single comparison is the one
above (``disagrees_source``'s semantics are pinned on
``StateEntry.disagrees_source``).

``kind`` (issue #246 review): ``"param"`` — a row built from the version's
params snapshot (the model emitted the value), ``"axis"`` — a row built
from the persisted per-axis stated set (the dimension protocol's W/D/H
axes). A param row and an axis row can both be named ``W`` (the model
emits a ``W`` param AND the user stated ``W``) — ``kind``+``name`` is the
row identity. The de-dup below (issue #316) is the ONE place this module
may drop a param row: a param that maps to an axis (declared ``axis`` or
literal W/D/H name) and AGREES with that axis's measured extent within the
bbox tolerance collapses into its axis row (the axis row keeps its
identity, the param row is not rendered). A param that DISAGREES keeps
both rows (the #264 disagrees display is unchanged); nothing else is ever
dropped or matched by name.

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

#: The threshold above which a model-source disagreement is "major" (issue #274).
#: A model-source disagrees row (``disagrees_source == "model"``) is
#: ``disagrees_major: True`` iff
#: ``|measured − stated_value| > max(DISAGREES_MAJOR_THRESHOLD_REL * stated_value,
#: DISAGREES_MAJOR_THRESHOLD_MIN_MM)``.  ``stated_value`` is the model's own
#: number (the row's ``stated_value`` field), NOT the measured value.
#
#: This pair is INDEPENDENT of ``BBOX_TOLERANCE_REL`` / ``BBOX_TOLERANCE_MIN_MM``
#: in ``d33d.design_loop`` (1 % / 0.5 mm).  The bbox tolerance decides whether a
#: value is ``measured`` vs ``disagrees`` at all;  this pair decides ``quiet``
#: vs ``major`` within a ``disagrees`` row.  Do NOT unify them.
DISAGREES_MAJOR_THRESHOLD_REL: float = 0.20
DISAGREES_MAJOR_THRESHOLD_MIN_MM: float = 5.0

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
    ``stated_value`` rides alongside a ``disagrees`` row (the displayed
    value is the MEASURED one — what will print — and the stated value
    rides alongside; they are never collapsed). It may also be present
    on a ``stated`` axis row collapsed into by an agreeing param (the
    collapse never strips it), so it is ``NotRequired``: the common
    case carries no ``stated_value`` key at all.
    ``measured_value`` rides alongside a ``stated`` row whose measurement
    confirmed the stated value within tolerance (issue #390 — the
    displayed value is the STATED number; the measured extent rides
    along as audit evidence).
    """

    name: str
    kind: RowKind
    label: str
    value: float | str | bool | None
    unit: str | None
    provenance: Provenance
    stated_value: NotRequired[float | str | bool | None]
    #: The measured extent that confirmed a stated value within tolerance
    #: (issue #390). Present on ``stated`` rows whose value was confirmed
    #: by a render; the displayed ``value`` is the stated number, and
    #: this field carries the measurement as the ride-along evidence.
    measured_value: NotRequired[float | None]
    #: Present ONLY on param rows with ``provenance == "disagrees"``
    #: (mirroring ``stated_value``): ``"user"`` when the stated value is
    #: the user's (the default — absent on the wire for backward
    #: compat), ``"model"`` when the value is the model's own assumed
    #: number (issue #264 — the Brief renders the model copy, never
    #: "You asked for"). Axis rows never carry it.
    disagrees_source: NotRequired[Literal["model", "user"]]
    #: ``True`` when the label is the raw SCAD identifier (no model label)
    #: — the UI renders it in the mono face (mono = machine value).
    label_is_identifier: NotRequired[bool]
    #: The model's declared axis (``"W" | "D" | "H"``) — the ONLY
    #: promotion evidence (issue #248). Present on param rows only. (A bare
    #: ``str``, not a ``Literal`` — the field is set only from the model's
    #: ``parameters`` metadata, which is shape-checked before it is stored,
    #: so a real row never carries an out-of-set value.)
    axis: NotRequired[str]
    #: The model's stated reason for a value the user did not give
    #: (issue #248) — the Brief's expanded assumed row renders it.
    reason: NotRequired[str]
    #: Present ONLY on model-source disagrees param rows (issue #274):
    #: ``True`` when ``|measured − stated_value| > max(
    #: DISAGREES_MAJOR_THRESHOLD_REL * stated_value,
    #: DISAGREES_MAJOR_THRESHOLD_MIN_MM)`` (the discrepancy is beyond the
    #: quiet threshold — render ochre), ``False`` otherwise (render the
    #: quiet neutral ``MARKS.measured`` style).  ``stated_value`` is the
    #: model's own number on the row.  Omitted on every other row (axis
    #: rows, user-source disagrees rows, non-disagrees rows).
    disagrees_major: NotRequired[bool]


def _is_number(value: Any) -> bool:
    """True for int/float (bool is excluded — it is not a measurement)."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


#: The name tokens that mark an angle parameter (issue #390): a token
#: equal to one of these, or a token that ENDS with "angle" (e.g.
#: "flareangle"), makes the unit "deg" — unless the name's last token is
#: a length word (``_LENGTH_WORDS``: ``angled_wall_thickness`` is a
#: thickness, not an angle).
_ANGLE_TOKENS: frozenset[str] = frozenset(
    ("angle", "deg", "degree", "degrees", "tilt", "draft")
)

#: The last tokens that mark a LENGTH parameter — a name ending in one
#: of these is "mm" even when it contains an angle token (issue #390).
#: The bare "triangle side" name ends in "side": a polygon side is a
#: length, never an angle (issue #390 operator decision: 'triangle_side'
#: → mm).
_LENGTH_WORDS: frozenset[str] = frozenset(
    (
        "thickness",
        "width",
        "depth",
        "height",
        "length",
        "radius",
        "diameter",
        "offset",
        "gap",
        "wall",
        "side",
    )
)

#: The last tokens (and the ``n_`` / ``num_`` name prefixes) that mark a
#: COUNT parameter — a count is unitless (``unit: None``, rendered as a
#: bare number), never "mm" (issue #390).
_COUNT_WORDS: frozenset[str] = frozenset(
    (
        "count",
        "n",
        "num",
        "number",
        "segments",
        "sides",
        "steps",
        "copies",
        "teeth",
        "ribs",
        "holes",
    )
)
_COUNT_PREFIXES: tuple[str, str] = ("n_", "num_")


def _split_name_tokens(name: str) -> list[str]:
    """The name's tokens: split on ``_``, then each piece on camelCase
    boundaries (``draftAngle`` → ``["draft", "Angle"]`` — lowercased in
    the caller). Empty pieces are dropped."""
    pieces: list[str] = []
    for raw in name.lower().split("_"):
        if not raw:
            continue
        token = ""
        for i, ch in enumerate(raw):
            if ch.isupper() and i > 0:
                pieces.append(token)
                token = ""
            token += ch.lower()
        if token:
            pieces.append(token)
    return pieces


def _is_angle_token(token: str) -> bool:
    """True when ``token`` is an angle mark: equal to one of
    ``_ANGLE_TOKENS`` or ending in "angle" — EXCEPT the operator's
    negative example "tangle" (issue #390: "tangle" → mm, not a token
    match; it has no token equal to an angle word and does not end in
    "angle")."""
    if token == "tangle":
        return False
    return token in _ANGLE_TOKENS or token.endswith("angle")


def _infer_unit(name: str) -> str | None:
    """The unit a numeric parameter's NAME implies (issue #390).

    Token-based: split the name on ``_`` and camelCase. The angle mark is
    the name's END (a last token that is an angle mark); a length word in
    last position is never deg; a count (last token in ``_COUNT_WORDS``
    or an ``n_`` / ``num_`` prefix) is unitless (``None``); everything
    else is "mm". The param_meta join (later) can override whatever this
    returns.
    """
    lower_name = name.lower()
    tokens = _split_name_tokens(name)
    last = tokens[-1] if tokens else lower_name
    if last in _LENGTH_WORDS:
        return "mm"  # a length word last is never deg
    if _is_angle_token(last):
        return "deg"
    if last in _COUNT_WORDS or lower_name.startswith(_COUNT_PREFIXES):
        return None
    return "mm"


def _entry(
    name: str,
    value: Any,
    provenance: Provenance,
    stated_value: Any = None,
    kind: RowKind = "param",
    label_is_identifier: bool = True,
) -> dict[str, Any]:
    """One entry from a (name, value) pair (provenance supplied)."""
    # Issue #390: the unit is inferred from the parameter name (token-
    # based: angles "deg", counts unitless, everything else "mm"); a
    # non-numeric value is unitless. The param_meta join happens later
    # and an explicit meta unit wins (never over a provenance-specific
    # unit — axis rows are "mm" by construction, and ``state_block_for_
    # version`` never rewrites a param row's unit).
    unit = _infer_unit(name) if _is_number(value) else None
    out: dict[str, Any] = {
        "name": name,
        "kind": kind,
        "label": name,  # label IS the parameter name (no invented prose)
        "label_is_identifier": label_is_identifier,
        "value": value,
        "unit": unit,
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
            # Issue #390 item 3: an explicit param_meta unit ALWAYS wins
            # over the name-based inference (angles and counts included).
            # It never overrides a provenance-specific unit — the only
            # other unit source is the inference itself (axis rows are
            # "mm" by construction via ``_axis_row`` and their meta name
            # never joins a param, so they are untouched here).
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
    """An axis row (name = W/D/H) for the design-state block.

    ``label_is_identifier`` is ``False`` (issue #274): the SPA renders the
    axis row's label as the English word Width/Depth/Height via
    ``copy.brief.axisLabel``, so the UI face (not the mono face) is
    correct for the label; the measured VALUE still renders in the mono
    face (the value's font is independent of the label's font in the SPA).
    """
    return _entry(axis, value, provenance, stated_value, kind="axis", label_is_identifier=False)


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

    The MEASUREMENT comparison (issue #137) applies to a param literally
    named W/D/H that the snapshot carries — regardless of the persisted
    stated set (within the named tolerance the param row renders
    ``measured`` with the measured value displayed; outside it
    ``disagrees`` with the stated value riding along, source
    ``disagrees_source: "user"`` — the #137 path always names ``"user"``
    explicitly: the user's stated value is what the measurement
    contradicts, never the model's own assumption). The persisted stated
    set only drives the SEPARATE axis rows; the two surfaces are
    independent and never name-matched.

    AXIS ROWS (issue #264 — "measured rows always"): for every axis whose
    measured extent is positive the block carries a W/D/H row, in W, D, H
    order, FIRST (before the param rows — the entry list order is the
    prompt order): a stated axis keeps today's rule (no measurement →
    ``stated``; within tolerance → ``measured`` with the measured value
    displayed; outside → ``disagrees`` carrying both numbers, never
    ``disagrees_source`` — an axis-row disagreement is always
    user-stated). An UNSTATED axis with a positive measured extent gets a
    row with provenance ``measured`` and the measured value (the part's
    real extent, never a fabricated number). Zero or absent extents
    abstain (no row), as today — ``persisted_bbox_extents`` stays
    all-or-nothing.

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

    MODEL-SOURCE DISAGREEMENT (issue #264): an ASSUMED param whose
    metadata declares an axis (``axis``, issue #248) — one the user never
    stated and never confirmed — is compared against that axis's measured
    extent: within tolerance it stays ``assumed`` (the measurement does
    not promote it); OUTSIDE tolerance it renders ``disagrees`` with the
    MEASURED value displayed, the param's own value as ``stated_value``,
    and ``disagrees_source: "model"`` (the Brief renders the model copy
    — the user never asked for the value). This model-source path is the
    only route for an ``assumed`` declared-axis param; the measurement
    honesty comparison above is the only route for a promoted one, and
    a param is never compared twice. Only positive numeric values enter
    either comparison.
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

    # Axis rows FIRST (issue #264 — the operator decision: axis rows
    # come first in W/D/H order, in both the entry list and the Brief):
    # every axis with a positive measured extent gets a row. A stated
    # axis keeps today's rule (stated / measured / disagrees, where
    # disagrees is always user-stated — axis rows never carry
    # ``disagrees_source``); an unstated axis with a positive measured
    # extent gets a row with provenance ``measured`` and the measured
    # value (the part's real extent — never a fabricated number).
    out: list[dict[str, Any]] = []
    for axis in AXIS_PARAM_NAMES:
        if extents is None:
            # No measurement: only a stated axis yields a row.
            if axis in evidence:
                out.append(_axis_row(axis, evidence[axis], "stated"))
            continue
        extent = extents[AXIS_PARAM_NAMES.index(axis)]
        if axis in evidence:
            axis_value = evidence[axis]
            tol = max(BBOX_TOLERANCE_REL * axis_value, BBOX_TOLERANCE_MIN_MM)
            if abs(extent - axis_value) <= tol:
                # Issue #390: an agreeing stated axis keeps provenance
                # "stated" (the user's number is displayed); the measured
                # extent rides along in ``measured_value`` (the render
                # confirmed it — audit evidence, not the display).
                row = _axis_row(axis, axis_value, "stated")
                row["measured_value"] = extent
                out.append(row)
            else:
                out.append(_axis_row(axis, extent, "disagrees", axis_value))
        else:
            # No stated evidence for this axis, but the measurement
            # carries it: emit the measured row (issue #264 — the
            # Brief always shows the part's measured W/D/H).
            out.append(_axis_row(axis, extent, "measured"))

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
        # the stated (confirmed) value riding along (user-sourced —
        # ``disagrees_source`` absent, the backward-compatible default).
        name = entry["name"]
        if extents is not None and name in AXIS_PARAM_NAMES:
            stated_value = entry["value"]
            if _is_number(stated_value) and stated_value > 0:
                extent = extents[AXIS_PARAM_NAMES.index(name)]
                tol = max(BBOX_TOLERANCE_REL * stated_value, BBOX_TOLERANCE_MIN_MM)
                e = dict(entry)
                if abs(extent - stated_value) <= tol:
                    if entry["provenance"] == "stated":
                        # Issue #390: a STATED W/D/H-named param that
                        # agrees with its measurement keeps provenance
                        # "stated" (the user's number is displayed);
                        # the measured extent rides along in
                        # ``measured_value``.
                        e["measured_value"] = extent
                    else:
                        # Non-stated W/D/H-named param (assumed/unknown)
                        # that agrees → measured (the #137 path, unchanged
                        # for non-stated rows).
                        e["value"] = extent
                        e["provenance"] = "measured"
                else:
                    e["value"] = extent
                    e["provenance"] = "disagrees"
                    e["stated_value"] = stated_value
                    e["disagrees_source"] = "user"  # #137 path: user-sourced
                out.append(e)
                continue
            # The W/D/H-named param has no positive numeric value (it
            # renders ``unknown``): fall through to the declared-axis
            # comparison below — a model that declared ``axis: W`` on it
            # can still yield a model-source disagreement (its value is
            # the measurement's only candidate), never a user-source one.
        # Model-source disagreement (issue #264): an ASSUMED param whose
        # metadata declares an axis, compared against that axis's
        # measured extent. Within tolerance it stays ``assumed`` (no
        # promotion — the measurement does not promote); outside it the
        # row renders ``disagrees`` with the measured value displayed,
        # the param's own value riding along as ``stated_value``, and
        # ``disagrees_source: "model"`` (the user never stated it — the
        # Brief must never render "You asked for"). A param promoted to
        # ``stated`` (rule (a)/(b)) or rendered ``measured`` by the
        # W/D/H-named path above is never model-source (the user gave
        # the evidence); only positive numeric values enter the
        # comparison.
        axis = entry.get("axis")
        if extents is not None and entry.get("kind") == "param":
            value = entry.get("value")
            if entry.get("provenance") == "stated":
                # Measurement honesty (issue #264): a param promoted to
                # ``stated`` (rule (a) or (b)) that declared an axis is
                # compared against that axis's measured extent — the
                # user's own evidence, so any disagreement is user-sourced
                # (``disagrees_source`` pinned on StateEntry). A literal
                # W/D/H-named param is handled by the #137 comparison
                # above (the two paths never overlap — a param is never
                # compared twice).
                if axis in _VALID_META_AXES and _is_number(value) and value > 0:
                    extent = extents[AXIS_PARAM_NAMES.index(axis)]
                    tol = max(BBOX_TOLERANCE_REL * value, BBOX_TOLERANCE_MIN_MM)
                    if abs(extent - value) <= tol:
                        out.append(entry)  # stays stated, value unchanged
                        continue
                    e = dict(entry)
                    e["value"] = extent
                    e["provenance"] = "disagrees"
                    e["stated_value"] = value
                    e["disagrees_source"] = "user"
                    out.append(e)
                    continue
            elif (
                entry.get("provenance") == "assumed"
                and axis in _VALID_META_AXES
                and _is_number(value)
                and value > 0
            ):
                # Model-source disagreement (issue #264 — see the module
                # docstring for the full contract).
                extent = extents[AXIS_PARAM_NAMES.index(axis)]
                tol = max(BBOX_TOLERANCE_REL * value, BBOX_TOLERANCE_MIN_MM)
                if abs(extent - value) > tol:
                    e = dict(entry)
                    e["value"] = extent
                    e["provenance"] = "disagrees"
                    e["stated_value"] = value
                    e["disagrees_source"] = "model"
                    # Issue #274: the 20%/5 mm disagrees_major flag rides
                    # along (the helper below — quiet vs major WITHIN
                    # disagrees, independent of the bbox tolerance).
                    e["disagrees_major"] = _disagrees_major_flag(extent, value)
                    out.append(e)
                    continue
        out.append(entry)
    return _dedupe_agreeing_param_rows(out)


def _dedupe_agreeing_param_rows(
    entries: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Issue #316 — the param-into-axis de-dup (the ONE drop in this module).

    A param row that maps to an axis and AGREES with that axis row's
    value within the bbox tolerance is REDUNDANT: the axis row already
    carries the same number (the axis row keeps its identity, name, and
    kind). The param row is DROPPED; the axis row takes the param's
    human label (when one exists) and the STRONGER of the two
    provenances — rank ``stated`` > ``measured`` > ``assumed`` (the
    user's words never lose to the model's guess, and the measurement
    never loses to either when one of them is ``stated``). A param whose
    own provenance is ``disagrees`` or ``unknown`` is never collapsed
    (a disagreeing param keeps both rows — the #264 display is
    unchanged — and an ``unknown`` param has no value to agree with).
    An axis row's own ``disagrees`` is authoritative (the #264 display
    is unchanged) and is never overwritten.

    A param row collapses into its axis row only when ALL of these hold:

    - the param is NOT already ``disagrees`` (a disagreeing param keeps
      both rows — the #264 display is unchanged);
    - the param's name maps to an axis — its declared ``axis`` (the
      ``param_meta`` axis, issue #248) when present, otherwise its name
      when literally one of W/D/H (issue #137's literal-name path);
    - that axis's ROW exists and carries a positive numeric value (a
      measured extent OR a persisted stated value — both are positive;
      an axis row always carries a positive number, since zero/absent
      extents abstain and never yield a row);
    - the param's value is a positive number (never ``unknown`` / ``0`` /
      non-numeric) and ``abs(param_value − axis_value) <= max(
      BBOX_TOLERANCE_REL * axis_value, BBOX_TOLERANCE_MIN_MM)``.

    Everything else keeps its row: a disagreeing param (the #264 display
    is unchanged), a param with no axis mapping, a param whose axis has
    no row, and a param with a non-positive or non-numeric value. Only
    param-into-axis collapses — never the reverse (the axis row is the
    authoritative display). When several params collapse into the same
    axis, the LAST one wins (declaration order) — the axis row carries
    that param's label and provenance.
    """
    by_axis: dict[str, dict[str, Any]] = {}
    for e in entries:
        if e.get("kind") == "axis" and e.get("name") in AXIS_PARAM_NAMES:
            by_axis[e["name"]] = e
    dropped: set[int] = set()
    for e in entries:
        if e.get("kind") != "param":
            continue
        axis = e.get("axis")
        if axis not in AXIS_PARAM_NAMES:
            axis = e.get("name")
        if axis not in AXIS_PARAM_NAMES:
            continue
        value = e.get("value")
        if not _is_number(value) or value <= 0:
            continue
        # A disagreeing param keeps its own row (the #264 display is
        # unchanged) — the de-dup collapses only AGREEING params.
        if e.get("provenance") == "disagrees":
            continue
        axis_row = by_axis.get(axis)
        if axis_row is None:
            continue
        axis_value = axis_row.get("value")
        if not _is_number(axis_value) or axis_value <= 0:
            continue
        if abs(float(value) - float(axis_value)) > max(
            BBOX_TOLERANCE_REL * float(axis_value), BBOX_TOLERANCE_MIN_MM
        ):
            continue
        # The param agrees with its axis — drop the param row; the axis
        # row takes the param's label (when one exists) and the
        # STRONGER of the two provenances (``stated`` > ``measured`` >
        # ``assumed`` — the user's words never lose to the model's
        # guess; an axis row's own disagrees is never overwritten).
        label = e.get("label")
        if isinstance(label, str) and label:
            axis_row["label"] = label
        param_prov = e.get("provenance")
        axis_prov = axis_row.get("provenance")
        if param_prov == "stated" and axis_prov != "stated":
            axis_row["provenance"] = "stated"
        elif param_prov == "measured" and axis_prov == "assumed":
            axis_row["provenance"] = "measured"
        dropped.add(id(e))
    return [e for e in entries if id(e) not in dropped]


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


#: The disagreement-SEVERITY threshold (issue #274): a model-source
#: ``disagrees`` row is ``disagrees_major`` (ochre) when
#: ``|measured − stated_value| > max(DISAGREES_MAJOR_THRESHOLD_REL *
#: stated_value, DISAGREES_MAJOR_THRESHOLD_MIN_MM)`` and quiet (neutral
#: ``MARKS.measured`` style) at or below it.
#
#: INDEPENDENT of ``BBOX_TOLERANCE_REL`` / ``BBOX_TOLERANCE_MIN_MM``
#: (1% / 0.5 mm in ``d33d.design_loop``): that pair decides
#: ``measured`` vs ``disagrees`` (does the measurement contradict the
#: value AT ALL); this pair decides quiet vs major WITHIN ``disagrees``
#: (how large the model's miss is). They are different questions with
#: different magnitudes and must NEVER be unified.
def _disagrees_major_flag(measured: float, model_value: float) -> bool:
    """Issue #274 severity flag: ``True`` (major/ochre) when the
    measured extent is MORE than ``max(20% of the model's number, 5 mm)``
    away from the model's own number; ``False`` (quiet) at or below the
    threshold. Only called on positive numeric values (every disagrees
    comparison in this module enters on ``_is_number(v) and v > 0``).
    """
    tol = max(
        DISAGREES_MAJOR_THRESHOLD_REL * model_value,
        DISAGREES_MAJOR_THRESHOLD_MIN_MM,
    )
    return abs(measured - model_value) > tol


def build_design_state_block(
    entries: list[dict[str, Any]],
    max_entries: int = MAX_STATE_BLOCK_ENTRIES,
) -> dict[str, Any]:
    """The serialised block: entries bounded to ``max_entries`` with a
    drop-and-count overflow marker.

    The bound is enforced HERE (the single function both consumers call):
    at most ``max_entries`` entries reach the prompt (or the SPA). Past
    the bound the overflow is dropped and counted — axis rows are ALWAYS
    kept (issue #264: the measured W/D/H rows are the part's real
    extents, never trimmed); PARAM rows are trimmed first, from the tail
    of the param section, declaration order preserved. The block's
    ``dropped_count`` carries how many param entries were omitted.
    ``dropped_count == 0`` means nothing was dropped (the count line is
    omitted by the formatter).
    """
    axis_rows = [e for e in entries if e.get("kind") == "axis"]
    param_rows = [e for e in entries if e.get("kind") != "axis"]
    keep = max(0, max_entries - len(axis_rows))
    kept_params = param_rows[:keep]
    dropped = len(param_rows) - keep
    return {
        "entries": list(axis_rows) + list(kept_params),
        "dropped_count": dropped,
        "unit": "mm",
    }


def _provenance_suffix(entry: dict[str, Any]) -> str:
    """The prompt's provenance mark (issue #246 + #264): the model must
    be able to tell user-set values from its own guesses. ``stated`` and
    ``assumed`` carry a mark; ``measured``/``unknown`` keep their existing
    plain rendering. ``disagrees`` names WHO the compared value belongs
    to: a model-sourced disagreement (``disagrees_source == "model"`` —
    the model's own assumed number versus the measurement) renders "my
    value differs from the measurement" — never "stated by the user" (the
    user never said it); a user-sourced one (``disagrees_source`` absent
    or ``"user"``) renders "you stated this; the measurement differs".
    Axis rows never carry ``disagrees_source`` — an axis-row disagreement
    is always user-stated and uses the user-sourced wording."""
    provenance = entry.get("provenance")
    if provenance == "stated":
        return " (stated by the user)"
    if provenance == "assumed":
        return " (assumed — the user never set this)"
    if provenance == "disagrees":
        if entry.get("disagrees_source") == "model":
            return " (my value differs from the measurement)"
        return " (you stated this; the measurement differs)"
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


def _render_value_line(entry: dict[str, Any]) -> str:
    """One entry as ``label = value`` + the provenance mark (if any)."""
    return f"{_axis_prefix(entry)} = {_render_value(entry.get('value'))}{_provenance_suffix(entry)}"


def _render_value_inline(entry: dict[str, Any]) -> str:
    """One entry as ``label=value`` + the provenance mark (if any)."""
    return f"{_axis_prefix(entry)}={_render_value(entry.get('value'))}{_provenance_suffix(entry)}"


def _render_value(value: Any) -> str:
    """The entry's value as prompt text (``not specified`` for a null)."""
    if _is_number(value):
        return f"{value:g}"
    if value is None:
        return "not specified"
    return str(value)


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
        lines.append(_render_value_line(entry))
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
        parts.append(_render_value_inline(entry))
    return ", ".join(parts)
