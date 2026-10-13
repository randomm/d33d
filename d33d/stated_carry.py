"""Carry-forward stated-dimension resolution (issue #393).

The history and carry-forward resolution half of the dimension protocol,
split out of ``d33d.statement_extraction`` (which owns the per-message
extraction): ``latest_stated_dims_dict`` / ``carried_stated_set`` (the
carry-forward input readers), ``effective_stated_dims`` (the merge
helper) and ``resolve_stated_cues`` (the one-message cue resolution used
at the chat and finalize seams).

Issue #433 adds the FEATURE-side carry: ``StatedFeature`` records (the
stated feature sizes — ``a 5 mm hole``) extracted by
``d33d.statement_extraction.stated_features_from_messages`` and carried
across turns in the project-level ``carried_stated_features`` column
(sibling of ``carried_stated_dims`` — the W/D/H set). The feature set
lives alongside the axis set, never inside it: a feature size is never a
W/D/H axis (the #398/#413 guard), so the two stores are separate and
:func:`effective_stated_features` owns the feature supersession (a later
message restating the same noun+role replaces the earlier value; a
different feature or no feature clause leaves the earlier value).

Import graph (strictly unidirectional, issue #393): ``axis_lexicon`` ->
``triple_extraction`` -> ``statement_extraction`` -> this module ->
``dimension_protocol`` (never back).
"""

from __future__ import annotations

import json
import logging
from typing import Any

from d33d.axis_lexicon import (
    _FEATURE_NOUNS,
    DIMENSION_AXES,
    FEATURE_ROLE_TOKENS,
    Cues,
    classify,
)
from d33d.statement_extraction import (
    StatedFeature,
    _coerce,
    feature_slot,
    stated_axes_from_message,
    stated_features_from_messages,
)

logger = logging.getLogger(__name__)

__all__ = [
    "carried_stated_features",
    "carried_stated_set",
    "carry_stated_features_for_turn",
    "effective_stated_dims",
    "effective_stated_features",
    "latest_stated_dims_dict",
    "persist_carried_stated_features",
    "resolve_stated_cues",
]

#: The most carried feature records one project keeps (issue #433 review):
#: a stored set larger than this is truncated on read — the carry is a
#: handful of stated sizes, never an unbounded list.
MAX_CARRIED_FEATURES: int = 32


def carried_stated_features(row: Any) -> list[StatedFeature]:
    """The project-level carried stated-feature set (issue #433).

    The reader for the ``projects.carried_stated_features`` column (the
    JSON list of ``{noun, role, value}`` objects written at the end of
    every chat/finalize turn — the sibling of ``carried_stated_dims``).
    ``None`` row, an absent/NULL column, or corrupt JSON yields ``[]``
    (an honest empty set — never a crash: a stored-set failure degrades
    to "no stated features" and the extraction re-derives from the
    message, which is always the freshest evidence).
    """
    if row is None:
        return []
    raw = row.get("carried_stated_features")
    if not isinstance(raw, str) or not raw:
        return []
    try:
        loaded = json.loads(raw)
    except (ValueError, TypeError):
        return []
    if not isinstance(loaded, list):
        return []
    out: list[StatedFeature] = []
    for item in loaded:
        if len(out) >= MAX_CARRIED_FEATURES:
            break
        if not isinstance(item, dict):
            continue
        value = _coerce(item.get("value"))
        if value is None:
            continue
        # A stored row is re-validated on read: the noun must be a known
        # feature noun and the role either absent or in the closed role
        # vocabulary — a tampered or stale row never reaches the matcher.
        noun = item.get("noun")
        if not isinstance(noun, str) or noun not in _FEATURE_NOUNS:
            continue
        role = item.get("role")
        if role is not None and (not isinstance(role, str) or role not in FEATURE_ROLE_TOKENS):
            continue
        out.append(StatedFeature(noun=noun, role=role, value=value))
    return out


def persist_carried_stated_features(
    conn: Any, project_id: int, features: list[StatedFeature]
) -> None:
    """Write the project's carried stated-feature set (issue #433) — the ONE
    persist helper for both the chat turn and the finalize turn.

    A failed write never fails the turn (the 202 / the loop still runs with
    the merged features in memory), but it is LOUD: a WARNING with the
    traceback, naming the project and the operation. The next turn then
    re-derives from the stored set it still has.
    """
    try:
        conn.raw.execute(
            "UPDATE projects SET carried_stated_features = ? WHERE id = ?",
            (
                json.dumps(
                    [{"noun": f.noun, "role": f.role, "value": f.value} for f in features]
                )
                if features
                else None,
                project_id,
            ),
        )
        conn.commit()
    except Exception:
        logger.warning(
            "carried_stated_features WRITE failed for project %s "
            "(operation: persist carried stated features); the turn continues "
            "with the merged features in memory",
            project_id,
            exc_info=True,
        )


def carry_stated_features_for_turn(
    conn: Any, project_id: int, message: str
) -> list[StatedFeature]:
    """The per-turn feature carry (issue #433): read the stored set, merge the
    message's features over it, persist the result, and return the merged set
    the turn uses (the loop's ``stated_features`` and the design-state
    comparison).

    A failed READ degrades without losing the current message: the message's
    own features are still merged (over an empty carry) and returned, and NO
    write happens — the stored set is kept rather than overwritten with a
    guess. A failed READ is a WARNING naming the project and the operation.
    """
    try:
        carried = carried_stated_features(conn.get_project(project_id))
    except Exception:
        logger.warning(
            "carried_stated_features READ failed for project %s "
            "(operation: read carried stated features); merging the message's "
            "features only, stored set left unchanged",
            project_id,
            exc_info=True,
        )
        return effective_stated_features([], message)
    fresh = effective_stated_features(carried, message)
    persist_carried_stated_features(conn, project_id, fresh)
    return fresh


def effective_stated_features(
    carried: list[StatedFeature],
    message: str,
) -> list[StatedFeature]:
    """The carry-forward merge for stated FEATURE sizes (issue #433).

    The one-message feature merge, mirroring :func:`effective_stated_dims`
    for the W/D/H set: extract the message's stated features
    (:func:`stated_features_from_messages` over the single message) and
    apply the operator supersession rule on top of the carried set — a
    restatement of the SAME feature (same ``noun`` + ``role``) replaces
    the earlier value; a different feature, or no feature clause, leaves
    the carried value in place. The result is the fresh set the caller
    persists (``projects.carried_stated_features``) and the design-state
    matcher compares against the model's params.

    ANY failure in the extraction degrades to the carried set unchanged
    (the conservative rule from :func:`resolve_stated_cues` — a failed
    extraction must never drop a carried feature statement), with a
    length-only warning (no message text — no PII in logs).
    """
    try:
        fresh = stated_features_from_messages([message])
    except Exception:
        logger.warning(
            "stated-features extraction failed (len(message)=%d) — "
            "carrying the stated feature set unchanged",
            len(message),
            exc_info=True,
        )
        return list(carried)
    if not fresh:
        return list(carried)
    # A restated SLOT (noun + size/role, see ``feature_slot``) replaces its
    # carried group AS A WHOLE, so every value the message stated for that slot
    # survives ("a 5 mm hole and a 8 mm hole" keeps both) — a per-key dict
    # would collapse them.
    fresh_keys = {feature_slot(f) for f in fresh}
    kept = [f for f in carried if feature_slot(f) not in fresh_keys]
    return kept + list(dict.fromkeys(fresh))


def latest_stated_dims_dict(versions: Any, project_id: int) -> dict[str, float] | None:
    """The latest version's persisted per-axis ``stated_dims`` (a dict of
    positive values), or ``None`` — the carry-forward merge's input
    (issue #261). Read via ``latest_version`` (the row's column is
    already JSON-decoded to a dict); values are filtered to positive
    floats (``axes_to_gate_triple``'s cleaning rule — a 0.0 persisted
    axis is unconfirmed, never a carried statement).

    Moved here from ``d33d.projects`` (issue #261 fix batch) so it sits
    next to :func:`effective_stated_dims`, the helper that consumes it;
    the three call sites (chat ``post_chat``, finalize
    ``versions_routes`` and region edit ``app``) import it from here.
    """
    latest = versions.latest_version(project_id)
    if latest is None:
        return None
    raw = latest.get("stated_dims")
    if not isinstance(raw, dict):
        return None
    axes: dict[str, float] = {}
    for axis, value in raw.items():
        try:
            f = float(value)
        except (TypeError, ValueError):
            continue
        if f > 0:
            axes[str(axis)] = f
    return axes or None


def carried_stated_set(
    conn: Any, versions: Any, project_id: int
) -> dict[str, float] | None:
    """The project-level carried per-axis stated set (issue #312, task-a).

    The SINGLE READER for the carry-forward input that feeds all four
    production seams (chat ``post_chat``, finalize ``versions_routes``
    (both sites) and region edit ``app.create_region_edit``). Replaces
    :func:`latest_stated_dims_dict`'s version-row-only read with the
    project-level ``carried_stated_dims`` column (written at the end of
    every chat/finalize turn — pass or fail — so a failed turn's stated
    axes survive into the next successful version).

    Seed: when the column is NULL (a fresh project, or an existing DB
    migrated before this column landed), the latest version's
    ``stated_dims`` is used as the fallback (the pre-task-a behaviour).
    Returns ``None`` when neither source provides a set (the merge helper
    treats ``None`` as an empty carried set — the gate abstains).

    Values are filtered to positive floats (the same cleaning rule as
    :func:`latest_stated_dims_dict`).
    """
    row = conn.get_project(project_id)
    if row is not None:
        raw = row.get("carried_stated_dims")
        if isinstance(raw, str) and raw:
            try:
                loaded = json.loads(raw)
            except (ValueError, TypeError):
                loaded = None
            if isinstance(loaded, dict):
                axes: dict[str, float] = {}
                for axis, value in loaded.items():
                    try:
                        f = float(value)
                    except (TypeError, ValueError):
                        continue
                    if f > 0:
                        axes[str(axis)] = f
                return axes or None
        elif isinstance(raw, dict):
            axes = {}
            for axis, value in raw.items():
                try:
                    f = float(value)
                except (TypeError, ValueError):
                    continue
                if f > 0:
                    axes[str(axis)] = f
            if axes:
                return axes
    # Fallback (seed from the latest version row when the column is NULL
    # or empty — a fresh DB or an un-migrated project).
    return latest_stated_dims_dict(versions, project_id)


def resolve_stated_cues(
    carried: dict[str, float] | None,
    message: str,
    *,
    label: str,
    project_id: int | None = None,
) -> dict[str, float]:
    """The carry-forward merge for ONE message (issue #369 round 2):
    try the per-axis extraction (``stated_axes_from_message``), fall back
    to the lexicon's ``classify`` (the ``Cues`` — absolute + relative +
    global) when the extraction yields no axis (a relative-only message
    like "make it taller" states nothing on its own but RELEASES the
    carried axis — semantics a bare dict cannot express), then merge into
    the carried set via :func:`effective_stated_dims`. ANY failure in the
    statement extraction OR the lexicon fallback degrades to the carried
    set unchanged (no release, no override — a deliberate, conservative
    choice: a failed classification must never manufacture a release or an
    override), with a WARNING that carries lengths only (no message text —
    no PII in logs). Used at the chat seam (``chat_loop``) and both
    finalize seams (``versions_routes``).
    """
    try:
        _am = stated_axes_from_message(message)
        _cues_arg = _am if _am else classify(message)
    except Exception:
        # A cue-resolution failure degrades to the carried set unchanged
        # (no release, no override — the conservative outcome). The
        # warning carries lengths only (no message text — no PII in
        # logs); ``project_id`` may be None (not always known at the
        # call site).
        logger.warning(
            "%s: stated-axes cue resolution failed; carrying the "
            "latest stated set unchanged (project_id=%s, len(message)=%d)",
            label,
            project_id,
            len(message),
            exc_info=True,
        )
        return effective_stated_dims(carried, None)
    return effective_stated_dims(carried, _cues_arg)


def effective_stated_dims(
    latest_stated: dict[str, float] | None,
    cues: dict[str, float] | Cues | None = None,
) -> dict[str, float]:
    """The carry-forward merge helper (issue #261's operator decision —
    ONE function, THREE call sites: chat ``post_chat``, finalize
    (``versions_routes``) and region edit (``app.create_region_edit``)).

    The effective per-axis stated set for the NEW version row — and, at
    chat and finalize, the gate input (``axes_to_gate_triple``) — computed
    in one place, never two divergent copies:

    * the set STARTS as the latest version's persisted ``stated_dims``
      (axes confirmed on an earlier turn are NOT stale — the user's
      "add a hole" keeps H=12 stated; #247's no-fallback rule dies);
    * a RELATIVE cue on an axis RELEASES that axis ("make it taller" →
      the gate must not enforce the old H — the #247 regression);
    * a GLOBAL cue releases ALL three ("make it bigger");
    * an ABSOLUTE cue SETS that axis ("make it 12 mm tall" → H=12, even
      when the latest row carried nothing or a different value —
      "H: 20" after {H: 12} states 20, the cue overrides the carried
      value);
    * axes with no cue carry forward unchanged.

    ``cues`` is one of: a ``d33d.axis_lexicon.Cues`` (the chat route's
    current-message classification), an explicit ``dict[str, float]``
    (a caller's body-stated or protocol-extracted set — treated as an
    absolute statement: the caller's explicit set OVERRIDES the carried
    set, per the precedence "body field > explicit protocol cues >
    lexicon", and carries no release semantics), or ``None`` / an empty
    set (the region-edit call site: the carried set comes back unchanged,
    no release). Precedence of cue kinds: the explicit set wins over the
    lexicon's absolute; releases and overrides compose.
    """
    carried: dict[str, float] = {}
    if latest_stated:
        for axis, value in latest_stated.items():
            try:
                f = float(value)
            except (TypeError, ValueError):
                continue
            if f > 0:
                carried[str(axis)] = f

    released: set[str] = set()
    absolute: dict[str, float] = {}

    if isinstance(cues, dict):
        # Explicit set (body field / protocol extraction): an absolute
        # override statement — no release semantics (a "W: 42" body does
        # not release H).
        for axis, value in cues.items():
            f = _coerce(value)
            if f is not None:
                absolute[str(axis)] = f
    elif cues is not None:
        absolute = dict(getattr(cues, "absolute", None) or {})
        released = set(getattr(cues, "relative", None) or ())
        if getattr(cues, "global_", False):
            released.update(DIMENSION_AXES)

    effective = {axis: v for axis, v in carried.items() if axis not in released}
    for axis, value in absolute.items():
        f = _coerce(value)
        if f is not None:
            effective[axis] = f
    return effective
