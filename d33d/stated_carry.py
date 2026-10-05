"""Carry-forward stated-dimension resolution (issue #393).

The history and carry-forward resolution half of the dimension protocol,
split out of ``d33d.statement_extraction`` (which owns the per-message
extraction): ``latest_stated_dims_dict`` / ``carried_stated_set`` (the
carry-forward input readers), ``effective_stated_dims`` (the merge
helper) and ``resolve_stated_cues`` (the one-message cue resolution used
at the chat and finalize seams).

Import graph (strictly unidirectional, issue #393): ``axis_lexicon`` ->
``triple_extraction`` -> ``statement_extraction`` -> this module ->
``dimension_protocol`` (never back).
"""

from __future__ import annotations

import json
import logging
from typing import Any

from d33d.axis_lexicon import DIMENSION_AXES, Cues, classify
from d33d.statement_extraction import (
    _coerce,
    stated_axes_from_message,
)

logger = logging.getLogger(__name__)

__all__ = [
    "carried_stated_set",
    "effective_stated_dims",
    "latest_stated_dims_dict",
    "resolve_stated_cues",
]


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
