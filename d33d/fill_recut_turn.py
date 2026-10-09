"""The fill-and-recut pre-route's turn handler (issue #332 sub-issue 3).

:func:`fill_recut_turn` is the fill-and-recut pre-route's ONE entry point —
the turn-level logic that ``d33d.fill_recut`` re-exports (``fill_recut``
keeps the trigger, the boundary copy, the offer instruction, and the
module-level regexes/word sets; the heavier per-turn handler lives here so
the offer module stays under its size budget). ``d33d.projects.post_chat``
awaits :func:`fill_recut_turn_async` (via the ``d33d.fill_recut`` re-export)
after the missing-source check and BEFORE the #250 offer / question
pre-routes.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from typing import Any

logger = logging.getLogger(__name__)

from d33d.fill_recut import (
    FILL_RECUT_DECLINE_REPLY,
    FRILL_POINT_AT_NO_DIM_TEMPLATE,
    FRILL_POINT_AT_TEMPLATE,
    _fmt_size,
    boundary_sentence,
    fill_and_recut_instruction,
    fill_recut_trigger,
    is_clean_no,
    is_clean_yes,
    no_match_hole_reply,
    own_feature_names,
)
from d33d.hole_select import _NO_MATCH as _NO_MATCH_HOLE
from d33d.hole_select import (
    holes_in_mm,
    report_bounds_mm,
    select_measured_hole,
)
from d33d.part_holes import HOLE_NOUNS, no_hole_reply, part_has_hole_evidence
from d33d.part_mesh import PartUploadError
from d33d.part_units import USABLE_UNIT_STATUSES

#: The off-loop legacy mesh load bound (issue #414, round 2): the stored
#: part mesh (up to ``MAX_PART_UPLOAD_BYTES`` / 2 M faces) is loaded via
#: ``asyncio.wait_for(asyncio.to_thread(...), timeout)`` so a pathological
#: parse cannot hold the turn open indefinitely. NOTE: ``to_thread`` runs
#: in a worker thread that is NOT cancellable — on timeout the loop stops
#: WAITING for the result (the turn degrades to the extents/2 fallback)
#: while the worker thread keeps running until the load finishes.
LEGACY_BOUNDS_LOAD_TIMEOUT_SECONDS = 20.0

#: The realistic exception set of a mesh load (issue #414, round 2):
#: ``PartUploadError`` / ``OSError`` from the read, ``ValueError`` / ``IndexError`` / ``KeyError`` /
#: ``AttributeError`` / ``TypeError`` from ``load_part_geometry`` on
#: exotic files (e.g. a 3MF ``Scene`` ``to_mesh()`` raising). Caught so a
#: load failure degrades to the extents/2 fallback with a logged warning
#: instead of 500-ing the chat route.
_MESH_LOAD_ERRORS: tuple[type[BaseException], ...] = (
    PartUploadError,
    OSError,
    ValueError,
    TypeError,
    AttributeError,
    IndexError,
    KeyError,
)


def _resolve_stored_part(app: Any, project_id: int) -> tuple[Any, Any, float]:
    """The legacy fallback's project row, committed part file path, and
    ``part_scale`` factor — resolved ON the calling (event-loop) thread:
    the app's sqlite handle is thread-bound (a read in a worker thread
    would raise ``sqlite3.ProgrammingError``).

    Returns ``(row, part_path, factor)``; ``part_path`` is ``None`` when
    the project has no committed part or the file is missing on disk;
    ``factor`` is ``part_scale`` as a positive float (``0.0`` for a
    missing/non-numeric/non-positive value — the caller keeps extents/2).
    An unreadable row (``sqlite3.Error``/``ValueError``) or a vanished
    file (``OSError``) degrades to ``(None, None, 0.0)`` — logged, never
    raised: the chat route must not 500 for a legacy report.
    """
    try:
        row = app.state.conn.get_project(project_id)
        if row is None:
            return None, None, 0.0
        from d33d.part_http import resolve_v1_part_path

        part_path, _repo_dir = resolve_v1_part_path(row, app.state.conn)
        factor = _scale_factor(row)
        if part_path is None or not part_path.exists():
            return row, None, factor
        return row, part_path, factor
    except (sqlite3.Error, ValueError, OSError) as e:
        logger.warning(
            "stored part bounds for project %s: row/part resolution "
            "failed (%s) — caller keeps the extents/2 fallback",
            project_id,
            type(e).__name__,
            exc_info=True,
        )
        return None, None, 0.0


def cache_stored_part_bounds(
    app: Any, project_id: int, file_bounds: list[list[float]]
) -> None:
    """Issue #414 (part 3): write the mesh-derived
    ``bbox_bounds_file_units`` back into the stored part report so the
    legacy fallback's mesh load happens ONCE per legacy project, not on
    every hole-noun turn.

    Runs on the calling (event-loop) thread (the sqlite handle is
    thread-bound). A missing row, a corrupt report (already degraded to
    ``None`` upstream), or a report that already carries the key is a
    no-op — the bounds are still usable this turn either way. A
    cache-write failure degrades to the uncached behaviour; never an
    error out of the chat route.
    """
    try:
        conn = app.state.conn
        row = conn.get_project(project_id)
        if row is None:
            return
        raw = row.get("part_report")
        report = json.loads(raw) if raw else None
        if not isinstance(report, dict) or "bbox_bounds_file_units" in report:
            return
        report["bbox_bounds_file_units"] = file_bounds
        conn.raw.execute(
            "UPDATE projects SET part_report = ? WHERE id = ?",
            (json.dumps(report), project_id),
        )
        conn.commit()
    except (sqlite3.Error, TypeError, ValueError) as e:
        logger.warning(
            "stored part bounds for project %s: cache write failed (%s)",
            project_id,
            type(e).__name__,
            exc_info=True,
        )


def _load_and_cache_mesh_bounds_sync(
    app: Any,
    project_id: int,
    part_path: Any,
    row: dict[str, Any],
) -> list[list[float]] | None:
    """The SYNC (direct) load step of the shared resolve → guard → load
    → cache sequence (issue #414, round 2): load the mesh directly on
    the calling thread, catching the full realistic mesh-load exception
    set, and cache the file bounds on success. The async path's load
    step is :func:`_load_and_cache_mesh_bounds_async` — same sequence,
    the load injected as an awaited, bounded off-loop load."""
    try:
        file_bounds = _load_mesh_bounds(part_path, row.get("part_format"))
    except _MESH_LOAD_ERRORS as e:
        logger.warning(
            "stored part bounds for project %s: mesh load failed (%s) "
            "— caller keeps the extents/2 fallback",
            project_id,
            type(e).__name__,
            exc_info=True,
        )
        return None
    if file_bounds is None:
        return None
    cache_stored_part_bounds(app, project_id, file_bounds)
    return file_bounds


async def _load_and_cache_mesh_bounds_async(
    app: Any,
    project_id: int,
    part_path: Any,
    row: dict[str, Any],
) -> list[list[float]] | None:
    """The ASYNC (off-loop, bounded) load step of the shared sequence
    (issue #414, round 2): ``wait_for(to_thread(_load_mesh_bounds),
    timeout)`` — the worker thread is NOT cancellable (on timeout the
    loop stops waiting and the turn degrades to the extents/2 fallback;
    the thread keeps running until the load finishes). Caches the file
    bounds on success; the cache write itself runs on the loop.
    Shared by :func:`load_mesh_bounds_async` and
    :func:`_resolve_legacy_bounds_async` so the load step has ONE home."""
    try:
        file_bounds = await asyncio.wait_for(
            asyncio.to_thread(_load_mesh_bounds, part_path, row.get("part_format")),
            timeout=LEGACY_BOUNDS_LOAD_TIMEOUT_SECONDS,
        )
    except _MESH_LOAD_ERRORS as e:
        logger.warning(
            "stored part bounds for project %s: mesh load failed (%s) "
            "— caller keeps the extents/2 fallback",
            project_id,
            type(e).__name__,
            exc_info=True,
        )
        return None
    if file_bounds is None:
        return None
    cache_stored_part_bounds(app, project_id, file_bounds)
    return file_bounds


async def load_mesh_bounds_async(app: Any, project_id: int) -> list[list[float]] | None:
    """The LEGACY-report fallback for :func:`d33d.hole_select.report_bounds_mm`
    — the stored part mesh's ``mesh.bounds`` (in FILE units, the same
    space the stored holes and ``bbox_file_units`` live in).

    Issue #414 (part 3): the row + part path resolve HERE, on the
    calling (event-loop) thread (sqlite is thread-bound), and ONLY the
    mesh load — up to ``MAX_PART_UPLOAD_BYTES`` / 2 M faces — goes off
    the loop via ``asyncio.wait_for(asyncio.to_thread(...), timeout)``,
    AWAITED on the loop, so the loop keeps serving other requests for
    the whole load (a thread + join on the calling thread would block
    the loop for the whole load — the reviewer's fake fix;
    ``asyncio.to_thread`` is the real one). Issue #414 (round 2): the
    load is BOUNDED by :data:`LEGACY_BOUNDS_LOAD_TIMEOUT_SECONDS`; on
    timeout the loop stops waiting and the turn degrades to the
    extents/2 fallback (the worker thread itself is not cancellable and
    keeps running until the load finishes — the documented trade-off of
    ``to_thread``).

    On success the file bounds are CACHED into the stored part report
    (:func:`cache_stored_part_bounds`), so a second turn takes
    ``report_bounds_mm``'s direct path and never loads the mesh again.

    ``None`` for every unavailable-mesh case (a 3MF import, a missing
    file, an unloadable file, an empty mesh, a missing v1 row, a
    missing / non-positive scale, a load timeout) — the caller keeps
    the old extents/2 behaviour; never an error out of the chat route.
    """
    row, part_path, _factor = _resolve_stored_part(app, project_id)
    if row is None or part_path is None:
        return None
    return await _load_and_cache_mesh_bounds_async(app, project_id, part_path, row)


def _load_mesh_bounds(part_path: Any, fmt: Any) -> list[list[float]] | None:
    """The stored part mesh's ``mesh.bounds`` in FILE units (XY-Z as two
    3-vectors — the ``report_bounds_mm`` / ``_numeric_vec`` contract).

    ``None`` when the mesh is empty (a 3MF import that loaded with no
    geometry); raises ``PartUploadError`` / ``OSError`` / the realistic
    mesh-load exception set when the file is unreadable or the mesh is
    unloadable — the caller degrades to the extents/2 fallback.
    """
    from d33d.part_http import MAX_PART_UPLOAD_BYTES
    from d33d.part_mesh import load_part_geometry, read_part_file_atomic

    raw = read_part_file_atomic(part_path, MAX_PART_UPLOAD_BYTES)
    mesh = load_part_geometry(raw, fmt or "stl")
    bounds = mesh.bounds
    if bounds is None:
        # A loadable mesh with no geometry (``bounds is None`` — the
        # empty-mesh case the docstring names): nothing to centre on.
        # Returning None (not raising) keeps the chat turn on the
        # extents/2 fallback instead of an uncaught ``TypeError`` from
        # the unpack.
        return None
    lo, hi = bounds
    return [[float(lo[0]), float(lo[1]), float(lo[2])],
            [float(hi[0]), float(hi[1]), float(hi[2])]]


def _scaled_part_bounds(
    file_bounds: list[list[float]], factor: float
) -> list[list[float]] | None:
    """``file_bounds`` scaled by ``factor``; ``None`` when the factor is
    non-positive — the caller keeps extents/2."""
    if factor <= 0:
        return None
    return [
        [v * factor for v in file_bounds[0]],
        [v * factor for v in file_bounds[1]],
    ]


def _scale_factor(row: dict[str, Any]) -> float:
    """The row's ``part_scale`` as a positive factor, or ``0.0``
    (missing/non-numeric/non-positive) — the caller keeps extents/2."""
    scale = row.get("part_scale")
    try:
        factor = float(scale) if scale is not None else 0.0
    except (TypeError, ValueError):
        factor = 0.0
    return factor if factor > 0 else 0.0


def _fill_recut_turn_body(
    app: Any,
    project_id: int,
    message: str,
    *,
    legacy_bounds_mm: list[list[float]] | None,
) -> dict[str, Any] | None:
    """The PLAIN sync turn body shared by :func:`fill_recut_turn` and
    :func:`fill_recut_turn_async` (issue #414, round 2: a coroutine shim
    driven with ``send``/``StopIteration`` is fragile and silently
    returns ``None`` if the body ever awaits — the body is now a plain
    function and the legacy bounds are resolved by the caller and
    passed in).

    ``legacy_bounds_mm`` is the already-resolved, SCALED legacy-report
    mesh bounds (``None`` keeps the extents/2 fallback).
    """
    from d33d.part_http import part_public

    row = app.state.conn.get_project(project_id)
    if row is None:
        return None
    part = part_public(row) if row.get("part_filename") else None
    if part is None or part.get("unit_status") not in USABLE_UNIT_STATUSES:
        return None

    versions = app.state.versions
    pending = versions.get_pending_offer(project_id)
    if pending is not None and pending.get("kind") == "fill_recut":
        # A LIVE fill-recut offer: "yes" clears the offer and runs the
        # loop with the explicit fill-and-recut instruction (the caller
        # registers the design loop and clears the offer once the event
        # source is up, restoring it on a setup failure — the acceptance
        # is never lost); "no" clears it and replies quietly
        # (nothing can fail after this point, so the clear is safe);
        # anything else supersedes the offer and re-evaluates this
        # message as a fresh turn.
        if is_clean_yes(message):
            return {
                "kind": "answer",
                "answer": None,
                "run_loop": True,
                "outcome": "accept",
                "instruction": fill_and_recut_instruction(pending),
                "accepted_offer": pending,
            }
        if is_clean_no(message):
            versions.set_pending_offer(project_id, None)
            return {
                "kind": "answer",
                "answer": FILL_RECUT_DECLINE_REPLY,
                "run_loop": False,
                "outcome": "decline",
            }
        # A new message supersedes the pending offer: clear it and
        # re-evaluate as a fresh turn.
        versions.set_pending_offer(project_id, None)
        pending = None

    if pending is None:
        trigger = fill_recut_trigger(message)
        if trigger is not None:
            own = own_feature_names(versions.latest_version(project_id))
            if trigger["noun"] not in own:
                # Issue #351 (operator decisions 1–4): a hole-family noun
                # is offered ONLY when the import stored hole evidence —
                # ``hole_count == 0`` → the honest no-hole reply (no
                # offer, no loop, no flag); ``None`` (unknown — no
                # report, legacy row, corrupt blob) keeps today's
                # behaviour. Other nouns keep current behaviour.
                if (
                    trigger["noun"] in HOLE_NOUNS
                    and part_has_hole_evidence(part) is False
                ):
                    return {
                        "kind": "answer",
                        "answer": no_hole_reply(trigger["noun"]),
                        "run_loop": False,
                        "outcome": "no_feature",
                    }

                # Issue #396: if the part has measured holes, select the
                # hole the user is referring to and store its geometry in
                # the offer so the instruction can carry the numbers.
                offer_dict: dict[str, Any] = {
                    "kind": "fill_recut",
                    "noun": trigger["noun"],
                    "size": trigger["size"],
                }
                sentence = boundary_sentence(
                    trigger["noun"],
                    trigger["size"],
                    move=trigger["move"],
                    move_distance_mm=trigger.get("move_distance"),
                    move_direction=trigger.get("direction"),
                )
                point_at_fallback = False
                no_match = False

                if trigger["noun"] in HOLE_NOUNS:
                    # Issue #396 (round 2): the stored holes are in FILE
                    # units; convert to mm via the part's scale BEFORE
                    # selection and instruction (the user's numbers are
                    # in mm — comparing file units against an mm trigger
                    # size and an mm bbox would pick the wrong hole).
                    # Issue #414: the centre reference is the part's
                    # REAL bounds (``bbox_bounds_file_units`` scaled by
                    # the same scale — extents/2 is only the
                    # origin-anchored special case).
                    report = part.get("report") if part else None
                    scale = part.get("scale")
                    holes = holes_in_mm(report, scale) if report else []
                    # Issue #414 (part 3): ``report_bounds_mm`` was
                    # called twice per turn — store the first result.
                    bounds_mm = report_bounds_mm(report, scale) if report else None
                    bbox_mm: list[float] | None = None
                    if bounds_mm is None and report is not None:
                        # Issue #414 (part 2): a LEGACY report has no
                        # ``bbox_bounds_file_units`` (``report_bounds_mm``
                        # → ``None``), and extents/2 is the part centre
                        # ONLY for origin-anchored parts — wrong for every
                        # off-origin part already imported (most real
                        # imports predate the fix). The caller has
                        # already resolved (and, for the async path,
                        # awaited off the event loop + cached) the mesh
                        # bounds — ``legacy_bounds_mm`` — or ``None``
                        # when the stored mesh is unavailable (a 3MF
                        # import, a missing file, a load timeout), in
                        # which case the old extents/2 behaviour (the
                        # origin-anchored special case) is kept.
                        bounds_mm = legacy_bounds_mm
                    if isinstance(report, dict):
                        bbox_fu = report.get("bbox_file_units")
                        # Guard the scale and bbox arithmetic against
                        # non-numeric stored values (issue #396 lens
                        # fix): treat them as unavailable rather than
                        # letting a corrupt row 500 the chat route.
                        try:
                            scale_f = float(scale) if scale is not None else 0.0
                        except (TypeError, ValueError):
                            scale_f = 0.0
                        if (
                            isinstance(bbox_fu, (list, tuple))
                            and len(bbox_fu) >= 2
                            and scale_f > 0
                        ):
                            try:
                                bbox_mm = [
                                    float(bbox_fu[0]) * scale_f,
                                    float(bbox_fu[1]) * scale_f,
                                ]
                            except (TypeError, ValueError):
                                bbox_mm = None
                    if holes:
                        selected = select_measured_hole(
                            holes, message, bbox_mm,
                            trigger_size=trigger["size"],
                            bounds_mm=bounds_mm,
                        )
                        if selected is None:
                            # Ambiguous or no qualifier matched: use the
                            # point-at copy (never an instruction without
                            # a location).
                            point_at_fallback = True
                        elif selected is _NO_MATCH_HOLE:
                            # Issue #414 (part 3): identity check against
                            # ``d33d.hole_select``'s module sentinel (the
                            # selector returns the same singleton for
                            # every no-match) instead of ``isinstance``.
                            no_match = True
                        else:
                            offer_dict["center"] = selected.get("center")
                            offer_dict["axis"] = selected.get("axis")
                            offer_dict["diameter_mm"] = selected.get("diameter_mm")

                if point_at_fallback:
                    dim_str = _fmt_size(trigger["size"])
                    if dim_str:
                        sentence = FRILL_POINT_AT_TEMPLATE.format(
                            noun=trigger["noun"], dim=dim_str
                        )
                    else:
                        sentence = FRILL_POINT_AT_NO_DIM_TEMPLATE.format(
                            noun=trigger["noun"]
                        )

                if no_match:
                    # Issue #414: the user named a hole by position but no
                    # measured hole matches — say so and list the measured
                    # holes (diameter + centre). No recut offer is made
                    # (nothing is stored server-side), so the done frame
                    # carries no offer flag (the ``outcome`` is not
                    # ``fresh_offer``) and no design loop runs.
                    sentence = no_match_hole_reply(trigger["noun"], holes)
                    return {
                        "kind": "answer",
                        "answer": sentence,
                        "run_loop": False,
                        "outcome": "no_match",
                    }

                versions.set_pending_offer(project_id, offer_dict)
                return {
                    "kind": "answer",
                    "answer": sentence,
                    "run_loop": False,
                    "outcome": "fresh_offer",
                }
    return None


def _report_is_legacy(report: dict[str, Any] | None) -> bool:
    """True when the stored report is a LEGACY report (issue #414):
    it exists but lacks ``bbox_bounds_file_units`` — the mesh-load
    fallback applies. ``None`` (no report) and cached reports are not
    legacy: the fallback must NOT load the mesh for them."""
    return isinstance(report, dict) and "bbox_bounds_file_units" not in report


def _resolve_legacy_bounds(
    app: Any,
    project_id: int,
    row: dict[str, Any] | None,
    report: dict[str, Any] | None,
) -> list[list[float]] | None:
    """Resolve the legacy-report mesh bounds for a turn (issue #414,
    round 2): one shared resolve → guard → load → cache sequence; the
    LOAD step is injected so the sync path loads directly on the
    calling thread and the async path awaits the off-loop (bounded)
    load. GATED on :func:`_report_is_legacy` — a cached or non-legacy
    report keeps the direct ``report_bounds_mm`` path and never loads
    the mesh. ``None`` keeps the extents/2 fallback. The part's
    ``part_scale`` factor is REUSED from :func:`_resolve_stored_part`
    (round 2, LOW: no recomputation via ``_scale_factor`` in the body)."""
    if not _report_is_legacy(report):
        return None
    resolved = _resolve_stored_part(app, project_id)
    if resolved[0] is None or resolved[1] is None:
        return None
    _row, part_path, factor = resolved
    if factor <= 0:
        return None
    file_bounds = _load_and_cache_mesh_bounds_sync(app, project_id, part_path, resolved[0])
    if file_bounds is None:
        return None
    return _scaled_part_bounds(file_bounds, factor)


async def _resolve_legacy_bounds_async(
    app: Any,
    project_id: int,
    row: dict[str, Any] | None,
    report: dict[str, Any] | None,
) -> list[list[float]] | None:
    """The async-path resolve (issue #414, round 2): row + path resolve
    ON the loop (sqlite is thread-bound), the load is AWAITED off the
    loop via :func:`_load_and_cache_mesh_bounds_async` (`wait_for
    (to_thread)`` — bounded by
    :data:`LEGACY_BOUNDS_LOAD_TIMEOUT_SECONDS`), and the cache write
    lands on the loop. GATED on :func:`_report_is_legacy`; same
    guard/scale contract as :func:`_resolve_legacy_bounds`."""
    if not _report_is_legacy(report):
        return None
    resolved = _resolve_stored_part(app, project_id)
    if resolved[0] is None or resolved[1] is None:
        return None
    _row, part_path, factor = resolved
    if factor <= 0:
        return None
    file_bounds = await _load_and_cache_mesh_bounds_async(
        app, project_id, part_path, resolved[0]
    )
    if file_bounds is None:
        return None
    return _scaled_part_bounds(file_bounds, factor)


async def fill_recut_turn_async(
    app: Any, project_id: int, message: str
) -> dict[str, Any] | None:
    """The ASYNC entry point for ONE fill-and-recut chat turn — the one
    the async ``d33d.projects.post_chat`` handler awaits (issue #414,
    part 3): the legacy-report mesh bounds are resolved HERE — row +
    path on the loop, the mesh load AWAITED off the loop via
    ``await asyncio.wait_for(asyncio.to_thread(...))`` (bounded by
    :data:`LEGACY_BOUNDS_LOAD_TIMEOUT_SECONDS`), the cache write on the
    loop — so a slow mesh parse never blocks the loop. The result is
    cached into the stored part report by the loader, so a second turn
    takes the direct ``report_bounds_mm`` path.

    The turn body itself is the plain :func:`_fill_recut_turn_body`
    (issue #414, round 2) — the legacy bounds are passed in as an
    argument.

    Returns the same ``{"kind": "answer", ...}`` dict (or ``None``)
    as :func:`fill_recut_turn`.
    """
    row = app.state.conn.get_project(project_id)
    if row is None:
        legacy_bounds = None
    else:
        from d33d.part_http import part_public

        part = part_public(row) if row.get("part_filename") else None
        report = part.get("report") if part else None
        legacy_bounds = await _resolve_legacy_bounds_async(app, project_id, row, report)
    return _fill_recut_turn_body(
        app, project_id, message, legacy_bounds_mm=legacy_bounds
    )


def fill_recut_turn(
    app: Any, project_id: int, message: str
) -> dict[str, Any] | None:
    """The SYNCHRONOUS entry point for ONE fill-and-recut chat turn —
    for non-async callers (the tests). The async chat route
    (``d33d.projects.post_chat``) awaits :func:`fill_recut_turn_async`.

    Resolves the legacy-report mesh bounds with a DIRECT sync loader
    (issue #414, round 2: no coroutine shim, no ``send`` /
    ``StopIteration`` — the body is the plain
    :func:`_fill_recut_turn_body` and the bounds are an argument) and
    calls the shared body.

    Returns ``{"kind": "answer", "answer": <sentence>, "run_loop": bool,
    "outcome": "fresh_offer" | "decline" | "accept" | "no_feature" | "no_match"}``
    when the turn is handled here, else ``None`` (fall-through to the
    existing routes).
    """
    row = app.state.conn.get_project(project_id)
    if row is None:
        legacy_bounds = None
    else:
        from d33d.part_http import part_public

        part = part_public(row) if row.get("part_filename") else None
        report = part.get("report") if part else None
        legacy_bounds = _resolve_legacy_bounds(app, project_id, row, report)
    return _fill_recut_turn_body(
        app, project_id, message, legacy_bounds_mm=legacy_bounds
    )


__all__ = [
    "fill_recut_turn",
    "fill_recut_turn_async",
]
