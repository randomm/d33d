"""The fill-and-recut pre-route's turn handler (issue #332 sub-issue 3).

:func:`fill_recut_turn` is the fill-and-recut pre-route's ONE entry point —
the turn-level logic that ``d33d.fill_recut`` re-exports (``fill_recut``
keeps the trigger, the boundary copy, the offer instruction, and the
module-level regexes/word sets; the heavier per-turn handler lives here so
the offer module stays under its size budget). ``d33d.projects.post_chat``
calls it (via the ``d33d.fill_recut.fill_recut_turn`` re-export) after the
missing-source check and BEFORE the #250 offer / question pre-routes.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import threading
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


def load_mesh_bounds_sync(app: Any, project_id: int) -> list[list[float]] | None:
    """The LEGACY-report fallback for :func:`d33d.hole_select.report_bounds_mm`
    — the stored part mesh's ``mesh.bounds`` (in FILE units, the same
    space the stored holes and ``bbox_file_units`` live in).

    Issue #414 (part 3): the row + part path resolve HERE, on the
    calling thread (sqlite is thread-bound), and ONLY the mesh load —
    up to ``MAX_PART_UPLOAD_BYTES`` / 2 M faces — goes off the loop.
    In an async context the load is dispatched through the loop's
    executor (``run_in_executor`` — the same executor ``asyncio.to_thread``
    uses) and awaited on a private worker thread, so the event loop is
    never blocked; in a sync context (no running loop — the tests) the
    load runs directly, bounded as before.

    On success the file bounds are CACHED into the stored part report
    (:func:`cache_stored_part_bounds`), so a second turn takes
    ``report_bounds_mm``'s direct path and never loads the mesh again.

    ``None`` for every unavailable-mesh case (a 3MF import, a missing
    file, an unloadable file, an empty mesh, a missing v1 row, a
    missing / non-positive scale) — the caller keeps the old extents/2
    behaviour; never an error out of the chat route.
    """
    row, part_path, factor = _resolve_stored_part(app, project_id)
    if row is None or part_path is None or factor <= 0:
        return None
    try:
        file_bounds = _run_mesh_load(part_path, row.get("part_format"))
    except (PartUploadError, OSError) as e:
        # An unloadable mesh or unreadable file degrades to the extents/2
        # fallback (3MF imports are not loadable here by design); logged
        # so a persistent degradation is greppable.
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


def _run_mesh_load(part_path: Any, fmt: Any) -> list[list[float]] | None:
    """``_load_mesh_bounds`` — dispatched off the loop via the running
    loop's executor when a loop is running (the ``post_chat`` case; the
    executor is exactly what ``asyncio.to_thread`` uses, and the caller
    is synchronous code ON the loop, so the wait happens on a private
    worker thread while the loop keeps serving), or directly otherwise
    (the sync/test case — the load is bounded, so the brief direct block
    is the documented degradation)."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return _load_mesh_bounds(part_path, fmt)
    result: list[list[float]] | None = None
    done = threading.Event()

    def _worker() -> None:
        nonlocal result
        try:
            result = _load_mesh_bounds(part_path, fmt)
        finally:
            done.set()

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    done.wait()
    return result


def _load_mesh_bounds(part_path: Any, fmt: Any) -> list[list[float]] | None:
    """The stored part mesh's ``mesh.bounds`` in FILE units (XY-Z as two
    3-vectors — the ``report_bounds_mm`` / ``_numeric_vec`` contract).

    ``None`` when the mesh is empty (a 3MF import that loaded with no
    geometry); raises ``PartUploadError``/``OSError`` when the file is
    unreadable or the mesh is unloadable — the caller degrades to the
    extents/2 fallback.
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


def _scale_factor(row: dict[str, Any]) -> float:
    """The row's ``part_scale`` as a positive factor, or ``0.0``
    (missing/non-numeric/non-positive) — the caller keeps extents/2."""
    scale = row.get("part_scale")
    try:
        factor = float(scale) if scale is not None else 0.0
    except (TypeError, ValueError):
        factor = 0.0
    return factor if factor > 0 else 0.0


def fill_recut_turn(
    app: Any, project_id: int, message: str
) -> dict[str, Any] | None:
    """The fill-and-recut pre-route's ONE entry point for ONE chat turn
    (issue #332's sub-issue 3). Called by ``d33d.projects.post_chat``
    AFTER the missing-source check and BEFORE the #250 offer / question
    pre-routes (the unsettled-part guard runs first, upstream).

    Returns ``{"kind": "answer", "answer": <sentence>, "run_loop": bool,
    "outcome": "fresh_offer" | "decline" | "accept" | "no_feature" | "no_match"}``
    when the turn is handled here (the caller registers the sentence as a
    ``kind: "answer"`` done frame — and, when ``run_loop`` is True, runs
    the design loop with the ``instruction`` field appended to the
    request text), else ``None`` (fall-through to the existing routes).

    The ``outcome`` discriminator is the SAME contract the region-edit
    seam's :func:`d33d.fill_recut_region.fill_recut_region_edit` returns:
    the caller keys OFF ``outcome`` — the done frame's ``fill_recut_offer``
    flag (the SPA's [Yes, do that] / [Leave it] buttons) is set ONLY for
    ``fresh_offer``; a clean ``decline`` re-emitting it would re-render
    the buttons for an offer that no longer exists.

    Handled cases: a LIVE fill-recut offer (``kind: "fill_recut"``
    pending, server-side state) — a clean "yes" clears the offer and
    runs the loop with the explicit fill-and-recut instruction; a clean
    "no" clears it and replies quietly; anything else supersedes the
    offer and re-evaluates this message as a fresh turn; and a fresh
    trigger on an assumed/settled part — the boundary sentence plus the
    offer recorded server-side (``kind: "fill_recut"``), or the honest
    no-hole reply for a hole-family noun with explicit
    ``hole_count == 0`` (issue #351).
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
                        # imports predate the fix). Derive the bounds from
                        # the project's stored part mesh: ``mesh.bounds``
                        # (in FILE units — the same space the holes live
                        # in) scaled by the part's scale. The load runs
                        # OFF the event loop and is cached into the
                        # stored report (``load_mesh_bounds_sync``), so
                        # it happens once per legacy project. When the
                        # stored mesh is unavailable (a 3MF import, a
                        # missing file), keep the old extents/2 behaviour
                        # (the origin-anchored special case).
                        file_bounds = load_mesh_bounds_sync(app, project_id)
                        bounds_mm = _scaled_part_bounds(file_bounds, _scale_factor(row)) if file_bounds else None
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


__all__ = [
    "fill_recut_turn",
]
