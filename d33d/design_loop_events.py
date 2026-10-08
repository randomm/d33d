"""Design-loop background-task adapter for the chat wire (issue #54).

The single place that runs ``app.state.run_design_loop`` for a chat
message and adapts its outcome onto the SSE frame contract that
``d33d.streaming._stream_events`` (``GET /api/stream/{project_id}``) and
the SPA's ``ApiClient.streamEvents`` expect:

- ``("progress", {step, iteration?})`` for each design-loop stage
- ``("token", {text})`` — exactly ONE token frame per completed loop
- ``("progress", {step: "version-created", version_id})`` on a pass
- ``("done", {message})`` on completion
- ``("error", {message, reason?})`` on exhaustion (``reason`` = the
  structured ``failure_reason``, omitted when absent) or infra failure
  (no ``reason`` — there is no ``DesignResult`` to read one from)

The adapter is a plain async generator (never a coroutine):
``event_sources[project_id]`` stores the generator object, so the SSE
endpoint's ``async for`` iterates it directly. It catches broadly
(:func:`run_design_loop_with_events`) — an unhandled exception here would
escape the generator, kill the SSE stream mid-turn, and (with
``streaming.py``'s historical narrow catch) leave the client hanging with
no terminal frame. Every exit path therefore ends in a terminal
``done``|``error`` frame, and the in-flight flag (``app.state
.design_loop_inflight``) is released in a ``finally`` that covers pass,
exhausted, and exception alike.
"""

from __future__ import annotations

import asyncio
import base64
import inspect
import io
import json
import logging
import math
import re
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from PIL import Image

from d33d.design_loop import (
    DESIGN_LOOP_ATTEMPT_TIMEOUT_SECONDS,
    MAX_ITERATIONS,
    MODEL_UNCONFIGURED,
    RENDERER_IMAGE_STALE,
    BboxInfo,
    _bbox_target,
    _gate_selection_extents,
    scad_looks_valid,
)
from d33d.loop_timeout import AttemptTracker, archive_deadline
from d33d.render_worker import VIEWS, RenderResult

logger = logging.getLogger(__name__)

#: Fixed 1x1 transparent-PNG data URI — the fallback when a project has no
#: stored photo or the stored file is missing out-of-band. A design-loop
#: ``photo`` must be a data URI/URL string (``None`` would crash the LLM
#: message builder), so the constant is never replaced with ``None``.
#: (A 1x1 fully transparent PNG has exactly one distinct colour, which is
#: why the base64 decodes to a valid, non-empty image.)
EMPTY_PHOTO_DATA_URI = (
    "data:image/png;base64,"
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAA"
    "SUVORK5CYII="
)

#: Wall-clock deadline for the design loop's per-attempt LLM CALL (issue
#: #417), in seconds — the single named constant
#: :data:`d33d.design_loop.DESIGN_LOOP_ATTEMPT_TIMEOUT_SECONDS` (120 s)
#: imported into this module; the full two-tier timeout design is
#: documented in one place:
#: ``d33d.design_loop._await_with_per_attempt_deadline``. Must be read as
#: a module-level constant inside the adapter's wait loop (so tests can
#: ``monkeypatch.setattr`` it to a small value — the same pattern as
#: ``versions_routes._DRAIN_TIMEOUT_SECONDS``), never inlined.

#: The margin over the derived total (per-attempt × MAX_ITERATIONS, 360 s)
#: the adapter's OUTER SAFETY NET deadline adds (issue #417): 60 s of
#: headroom so a legitimately slow run that the loop's own per-attempt
#: deadline has already cut off (returning its best-so-far result through
#: the ordinary exhaustion path) is never cut off twice by the adapter.
#: The adapter deadline stays below the client's
#: ``STREAM_TOTAL_TIMEOUT_MS`` (``web/src/lib/api.ts``, 480 s) with margin
#: (360 + 60 = 420 s < 480 s) so the server's structured frame arrives
#: before the client's generic "stream interrupted" kill.
ADAPTER_DEADLINE_MARGIN_SECONDS = 60.0

#: The distinct structured reason code for a deadline-triggered terminal
#: error frame. Deliberately NOT the render-worker's ``"timeout"``
#: ``ErrorClass`` (``render_worker.py``'s 120s subprocess timeout) — the
#: SPA maps each to its own ``copy.failure.reasons`` entry, and reusing
#: ``"timeout"`` would show render-worker copy for a whole-loop stall
#: (issue #221).
DESIGN_LOOP_TIMED_OUT_REASON = "design_loop_timed_out"

#: MIME by file extension — the upload route (``d33d.projects.upload_photo``)
#: constrains photos to image/png and image/jpeg, so the mapping is total.
_PHOTO_MIME_BY_SUFFIX: dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
}

# Issue #299 — the decompression-bomb guard: an image claiming more pixels
# than this is rejected at header-parse time (before any full decode), so a
# tiny file with a 10000×1 header can never allocate gigabytes.
MAX_PHOTO_SIDE_PX = 8192
# NOTE: process-wide — this assignment also covers ``render_worker``'s
# PNG view checks (they run in the same process and import the same PIL
# module) as well as every photo check below.
Image.MAX_IMAGE_PIXELS = MAX_PHOTO_SIDE_PX * MAX_PHOTO_SIDE_PX


#: A photo upload whose bytes decode to this format is stored with ``.png``;
#: ``JPEG``/``JPEG2000``/``MPEG`` decode to ``.jpg`` (the upload gate only
#: admits PNG and JPEG, so the mapping is total over accepted bytes).
_FORMAT_TO_SUFFIX: dict[str, str] = {
    "PNG": ".png",
    "JPEG": ".jpg",
}


def validate_photo_bytes(content: bytes) -> str:
    """The upload decode gate (issue #299): ``content`` must be a
    decodable PNG or JPEG image; the returned ``.png``/``.jpg`` suffix
    follows the DETECTED format (never the declared content type).

    Raises ``ValueError`` for any undecodable payload, a non-PNG/JPEG
    format, a side exceeding :data:`MAX_PHOTO_SIDE_PX`, or a decode
    failure — the caller maps ``ValueError`` to HTTP 422. The dimensions
    are read from the header (``Image.open`` is lazy) and checked BEFORE
    ``verify()`` / ``load()``, so a decompression-bomb header is rejected
    without a full decode.
    """
    try:
        with Image.open(io.BytesIO(content)) as img:
            if img.format not in _FORMAT_TO_SUFFIX:
                raise ValueError(
                    f"not a PNG or JPEG image (decoded as {img.format!r})"
                )
            width, height = img.size
            if width > MAX_PHOTO_SIDE_PX or height > MAX_PHOTO_SIDE_PX:
                raise ValueError(
                    f"image is {width}x{height}; each side must be "
                    f"≤ {MAX_PHOTO_SIDE_PX} px"
                )
            img.verify()  # structural integrity, no full pixel decode
        with Image.open(io.BytesIO(content)) as img:
            img.load()
    except ValueError:
        raise
    except Exception as e:  # PIL raises OSError/Image.DecompressionBombError etc.
        raise ValueError(f"undecodable image: {e}") from e
    with Image.open(io.BytesIO(content)) as img:
        return _FORMAT_TO_SUFFIX[img.format]

def _photo_usable(photo_path: str) -> bool:
    """True iff the stored photo at ``photo_path`` is a decodable PNG or
    JPEG (issue #299) — the EMBED-TIME twin of the upload decode gate
    (the one shared definition of "a usable photo", built on the same
    checks as :func:`validate_photo_bytes` — full ``verify()`` AND
    ``load()``, so upload, embed and storage checks agree by
    construction: a truncated-IDAT file is "not usable" in all three).

    The design loop must never embed an undecodable file (the LLM endpoint
    400s on every design pass), so a present-but-undecodable photo
    degrades to the photo-LOST path instead: :func:`photo_data_uri`
    returns :data:`EMPTY_PHOTO_DATA_URI` and :func:`photo_lost` fires the
    #295 notice + WARNING. Any read/decode failure returns ``False`` —
    this helper never raises.
    """
    try:
        data = Path(photo_path).read_bytes()
        with Image.open(io.BytesIO(data)) as img:
            if img.format not in _FORMAT_TO_SUFFIX:
                return False
            img.verify()
        with Image.open(io.BytesIO(data)) as img:
            img.load()
    except (OSError, ValueError, SyntaxError):
        # undecodable, unreadable, oversized (or a bad-chunk-CRC ``
        # SyntaxError`` — PIL's PNG verify raises it, see
        # :func:`_photo_structurally_valid`) — all treated as lost
        return False
    return True


def photo_storage_signal(photo_path: str | None) -> bool | None:
    """The project's stored-photo storage signal (issue #299): the
    THREE-way ``photo_present`` value, computed CHEAPLY — a header-only
    open + dimension check + ``verify()`` (structural integrity), no full
    ``load()``, and memoized per ``(path, mtime, size)`` so the project
    list endpoint never pays a per-row decode.

    ``None`` for a photo-LESS project (``source_photo_path`` unset — a
    photo-LESS project must never read as a LOST one); ``False`` for a
    photo whose file is missing out-of-band OR present-but-undecodable;
    ``True`` when the stored file is on disk and structurally intact.

    Deliberately NOT :func:`_photo_usable` (which full-decodes at embed
    time, the strict twin of the upload gate): the storage signal only
    needs the cheap structural check — a file whose header and verify
    pass is reported present, and the embed-time gate remains the
    strict final word before anything reaches an LLM request.
    """
    if not photo_path:
        return None
    p = Path(photo_path)
    if not p.is_file():
        return False
    try:
        st = p.stat()
    except OSError:
        return False
    key = (photo_path, st.st_mtime, st.st_size)
    cached = _photo_signal_cache.get(key)
    if cached is not None:
        return cached
    ok = _photo_structurally_valid(photo_path)
    if len(_photo_signal_cache) > 1024:  # bound the memo (stale keys
        _photo_signal_cache.clear()  # out as files turn over)
    _photo_signal_cache[key] = ok
    return ok



def _photo_structurally_valid(photo_path: str) -> bool:
    """Cheap structural check: header open + side cap + ``verify()`` —
    NO full pixel decode (the ``load()`` the embed-time gate still
    performs). Any read/decode failure is ``False`` (never raises).

    The ``SyntaxError`` in the catch is deliberate: PIL's PNG ``verify``
    raises ``SyntaxError`` (not ``OSError``) for a bad chunk CRC — a
    structurally-broken PNG must report ``False`` (unusable) here, not
    escape the cheap check and 500 the project list endpoint.
    """
    try:
        with Image.open(io.BytesIO(Path(photo_path).read_bytes())) as img:
            if img.format not in _FORMAT_TO_SUFFIX:
                return False
            width, height = img.size
            if width > MAX_PHOTO_SIDE_PX or height > MAX_PHOTO_SIDE_PX:
                return False
            img.verify()
    except (OSError, ValueError, SyntaxError):
        return False
    return True


#: The storage-signal memo: ``(path, mtime, size) -> bool``. Files change
#: only out-of-band (or via a fresh upload, which sets a new path), so
#: the mtime+size key is the invalidation — the list endpoint pays at
#: most one header check per distinct file per session, not one per row.
_photo_signal_cache: dict[tuple[str, float, int], bool] = {}


def photo_data_uri(source_photo_path: str | None) -> str:
    """The project's stored photo as a data URI (MIME from the extension).

    ``None`` / missing file → :data:`EMPTY_PHOTO_DATA_URI` (never ``None`` —
    the design loop's LLM message builder requires a data URI/URL string).
    An on-disk-but-undecodable file returns the SAME constant (issue
    #299 — the MIME flips to ``image/png`` on the way down, which the
    notice/WARNING contract treats as expected); the URI contract stays
    EMPTY for ALL FOUR photo states (issue #295: the notice / WARNING
    logic lives at the ``post_chat`` caller — :func:`photo_lost` — never
    here).
    """
    if not source_photo_path:
        return EMPTY_PHOTO_DATA_URI
    p = Path(source_photo_path)
    if not p.is_file():
        return EMPTY_PHOTO_DATA_URI
    if not _photo_usable(source_photo_path):
        return EMPTY_PHOTO_DATA_URI
    mime = _PHOTO_MIME_BY_SUFFIX.get(p.suffix.lower(), "image/png")
    b64 = base64.b64encode(p.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{b64}"


def photo_lost(row: dict[str, Any]) -> bool:
    """Whether the project's stored photo must be treated as LOST (issue
    #295, extended by issue #299): ``source_photo_path`` is set AND the
    file is either gone out-of-band OR present-but-undecodable (a bad
    upload or out-of-band corruption — embedding it would 400 every
    design pass).

    The three-way photo state, exactly as the operator decided it:
    ``source_photo_path`` NULL (photo-LESS project) → ``False`` (the run
    proceeds identically to today — no notice, no WARNING); path set +
    file present AND decodable → ``False``; path set + file missing or
    undecodable → ``True`` (the stream carries the copy.ts notice and the
    WARNING fires — project id + the photo's FILE NAME; never a full
    path, never the bytes).
    """
    photo_path = row.get("source_photo_path")
    if not photo_path:
        return False
    p = Path(photo_path)
    if not p.is_file():
        return True
    return not _photo_usable(photo_path)


def axes_to_gate_triple(
    axes: dict[str, float] | None,
) -> tuple[float, float, float] | None:
    """The bbox gate's (W, D, H) target from one run's OWN per-axis
    confirmed set — issue #247's per-axis operator decision: the gate
    enforces ONLY the axes the current run's input confirmed.

    The return is the confirmed set NORMALIZED into a W/D/H triple with
    ``0.0`` for every unconfirmed axis — the #91 zero-means-unknown
    convention the loop's consumers already speak (``_bbox_within_tolerance``
    skips ``<= 0`` axes, ``_dim_axis_list`` renders them as
    ``not specified``). ``None`` ONLY when no axis at all is confirmed
    (abstain entirely — never a zero triple). Non-positive values are
    dropped like everywhere else in the per-axis pipeline.

    There is deliberately NO fallback to a persisted version row: a
    follow-up message with no explicit dimension cue ("make it taller",
    "make it 20 mm tall") confirms nothing — the gate abstains — even if
    an earlier version row persisted ``{"H": 12}``. Forcing the stale
    axis against a candidate the user just asked to change is exactly
    what regressed the gate (it failed every candidate → exhausted).
    Carry-forward of confirmed dimensions across turns is a separate
    product decision (issue #247's operator decision superseded the
    one-turn persisted-axes fallback).
    """
    if not axes:
        return None
    cleaned: dict[str, float] = {}
    for axis, value in axes.items():
        try:
            f = float(value)
        except (TypeError, ValueError):
            continue
        if f > 0:
            cleaned[str(axis)] = f
    if not cleaned:
        return None
    return (
        cleaned.get("W", 0.0),
        cleaned.get("D", 0.0),
        cleaned.get("H", 0.0),
    )


def latest_version_stated_dims(
    versions_service: Any, project_id: int
) -> tuple[float, float, float] | None:
    """The latest version's confirmed (W, D, H) triple, or ``None`` —
    issue #247's re-pointing of ticket #91's fallback source.

    Reads the PERSISTED per-axis confirmed set
    (``versions.stated_dims`` — the latest version row's column, written
    by the dimension protocol's per-axis extraction) and derives the
    triple from it. The return contract is the strict one — a full
    positive ``(W, D, H)`` triple or ``None`` — kept for the 3MF export
    route (``d33d.app`` → ``validate_stl`` ``stated_mm``): a full triple
    when ALL three axes are confirmed on the latest row, else ``None``
    (abstain). The loop-facing routes (chat, finalize, region-edit) use
    :func:`axes_to_gate_triple` instead — a PARTIAL confirmed set is a
    zero-filled triple, never ``None`` (the #247 per-axis decision).

    The dead W/D/H param-key read is GONE: a prior version whose ``params``
    snapshot happens to carry ``W``/``D``/``H`` keys but whose
    ``stated_dims`` is NULL (every pre-#246 row) yields ``None``, not a
    triple re-derived from param names the model never emitted.

    """
    latest = versions_service.latest_version(project_id)
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
    if "W" not in axes or "D" not in axes or "H" not in axes:
        return None
    return (axes["W"], axes["D"], axes["H"])


def _component_extent(component: Any) -> tuple[float, float, float, float, float, float, float]:
    """``(x_extent, y_extent, z_extent, volume, min_x, min_y, min_z)`` from
    one trimesh component (issue #100). All values are plain ``float``
    (numpy scalars are converted). Volume defaults to 0.0 for a
    non-watertight component (trimesh raises on ``.volume`` for
    non-watertight meshes — the gate only consumes volume as a best-match
    tie-breaker, never as a conformance signal, so a missing volume is
    harmless)."""
    b = component.bounds
    extents = (
        float(b[1, 0] - b[0, 0]),
        float(b[1, 1] - b[0, 1]),
        float(b[1, 2] - b[0, 2]),
    )
    try:
        volume = float(getattr(component, "volume", 0.0) or 0.0)
    except (ValueError, TypeError, RuntimeError):  # non-watertight component
        volume = 0.0
    mins = (
        float(b[0, 0]),
        float(b[0, 1]),
        float(b[0, 2]),
    )
    return (*extents, volume, *mins)


def bbox_from_render(render: RenderResult) -> BboxInfo | None:
    """Per-axis extents (mm) for the design-loop bbox gate, from a render.

    The intended design: ``render_for_design_loop`` trimesh-loads the
    harvested ``model.stl`` and computes ``vertex_count`` / ``watertight`` /
    ``volume_mm3`` from that mesh, so the extents derive from the same
    on-volume STL — re-loaded host-side here from ``render.stl``.

    ``render.stl`` is re-pointed at the durable artifact directory when
    the render worker's issue #72 persistence succeeds, so a live path is
    the normal production case: a successful load yields real extents. A
    ``None`` return is the edge case — persistence disabled or failed, or
    the file externally deleted — where ``path.is_file()`` is ``False`` →
    the bbox gate cannot score (the gate then fails and no candidate can
    score the bbox bit).

    Issue #100 — multi-part meshes: the bbox gate needs the mesh's
    per-component extents (a stated single-body triple must match the
    best-matching component, not the whole-assembly bbox), so this seam
    runs the merge+split internally and carries the breakdown on
    ``BboxInfo.components`` (``x``/``y``/``z``/``volume`` keep their
    whole-assembly meaning):

    **CRITICAL: ``merge_vertices()`` MUST run before
    ``split(only_watertight=True)`` — do NOT "optimise" it away.**
    The production STLs OpenSCAD emits are FACE-DISCONNECTED (triangles
    share no vertices), so on an unmerged load ``split()`` finds ZERO
    watertight connected components (measured on
    ``tests/fixtures/stl/box_20mm.stl``: unmerged → 0 components; merged
    → exactly 1 for the single 20mm body). Without the merge, every
    multi-part render would split to zero and fail the gate on a broken
    mesh that renders fine — the exact silent-corruption class issue #84
    already cost days to remove. Merging is NOT a no-op for bounds (the
    old "bounds are merge-invariant" note below is true for BOUNDS and
    false for SPLIT, which is why it is now corrected) — it is what makes
    ``split`` see a shell as one connected component.

    A component breakdown the split cannot produce (empty) still returns
    the whole-assembly ``BboxInfo`` (empty ``components`` → the gate's
    legacy whole-part path). A zero-component split on a non-empty mesh is
    a genuinely broken mesh: the gate fails it (``bbox_out_of_tolerance``)
    — it is never a vacuous pass (issue #100, PM decision 2).

    Returns ``None`` (the bbox gate fails) when the render has no STL or it
    cannot be loaded; never raises.
    """
    stl = render.stl
    if not isinstance(stl, str) or not stl:
        return None
    path = Path(stl)
    if not path.is_file():
        return None
    try:
        import trimesh

        mesh = trimesh.load(str(path), process=False)
        if not hasattr(mesh, "bounds") or mesh.bounds is None:
            return None
        b = mesh.bounds
        x = float(b[1, 0] - b[0, 0])
        y = float(b[1, 1] - b[0, 1])
        z = float(b[1, 2] - b[0, 2])
        volume = float(getattr(mesh, "volume", 0.0) or 0.0)

        # Issue #100: per-component breakdown for the multi-part gate.
        # merge_vertices() BEFORE split() — see the CRITICAL note above;
        # without it the production face-disconnected STL splits to ZERO
        # components (the whole-part ``components=()`` path would then
        # compare the stated triple against the whole-assembly bbox and
        # every multi-part request would fail). ``merge_vertices()`` is
        # destructive (mutates in place and returns self), so it is
        # called on the loaded mesh, not assigned to a throwaway. It is
        # INSIDE the load try: a failure here is a load failure (the
        # outer ``except`` → None), not a split failure.
        mesh.merge_vertices()
        # Initialize before the try so the except branch can reference it.
        components: tuple[tuple[float, float, float, float, float, float, float], ...] = ()
        try:
            split_result = mesh.split(only_watertight=True)
            # ``split()`` returns a LIST of submeshes (one per watertight
            # connected component); on trimesh 5.1.0 a mesh with zero
            # watertight components yields an empty list (the zero-component
            # case the gate must fail, not pass). Defensive: an unexpected
            # non-list shape degrades to an empty breakdown (the legacy
            # whole-part path), never a raise.
            if isinstance(split_result, list):
                if not split_result:
                    # Zero components: the mesh is genuinely broken
                    # (non-watertight even after merge). Return None so
                    # the gate FAILS — a vacuous pass here would repeat
                    # the exact defect class issue #84 removed.
                    #
                    # This None is deliberately the SAME outcome as the
                    # outer load-failure None (the gate bit is False either
                    # way — a broken mesh never passes), but the two log
                    # lines below are the ONLY place the conditions are
                    # told apart: "mesh loaded OK but split found 0
                    # watertight components" (healthy geometry, broken
                    # topology — a split-level problem, greppable) vs the
                    # "failed to load STL" line (the file/mesh itself
                    # would not load). Do not merge the messages: this
                    # codebase has been bitten by two distinct conditions
                    # collapsing into one indistinguishable signal.
                    logger.error(
                        "bbox_fn: mesh loaded OK but split found 0 watertight "
                        "components for %r — the bbox gate will fail "
                        "(bbox_out_of_tolerance), not pass",
                        stl,
                    )
                    return None
                components = tuple(
                    _component_extent(comp)
                    for comp in split_result
                )
        except (AttributeError, ValueError, TypeError, RuntimeError):
            # A split failure on a mesh that LOADED and whose bounds were
            # measured is a healthy-geometry anomaly, NOT a load failure —
            # degrading to an empty breakdown (the legacy whole-part path)
            # would silently re-create the exact issue #100 defect for
            # every multi-part mesh (the stated triple compared against
            # the whole-assembly bbox). Returning None fails the gate
            # loudly, indistinguishable-from-a-broken-mesh at the wire
            # level but greppable via this log line (which names the
            # consequence and the trimesh version, so a trimesh upgrade
            # that changes split() behaviour is diagnosable in one grep).
            # The tuple is the known trimesh failure surface (attribute
            # error on a changed API, value/type error from a non-list
            # shape, runtime error from the geometry itself); MemoryError
            # (BaseException subclass — a killed process is the right
            # fate for OOM) and KeyboardInterrupt/SystemExit are not
            # caught, deliberately.
            logger.error(
                "bbox_fn: component split failed for %r (trimesh %s) — "
                "degrading to no breakdown means the bbox gate will fail; "
                "refusing to fall back to the whole-part comparison that "
                "issue #100 removed",
                stl,
                getattr(trimesh, "__version__", "unknown"),
            )
            return None
        return BboxInfo(x=x, y=y, z=z, volume=volume, components=components)
    except Exception:  # any load failure → gate fails (None), never a raise
        logger.exception("bbox_fn: failed to load STL %r", stl)
        return None


def _data_uri_from_bytes(raw: bytes, mime: str) -> str:
    """A base64 data URI from raw bytes and a MIME type."""
    b64 = base64.b64encode(raw).decode("ascii")
    return f"data:{mime};base64,{b64}"


def _artifact_bytes_from_path(render: RenderResult) -> tuple[bytes | None, dict[str, bytes]]:
    """Read the best render's STL + view bytes from its DURABLE artifact
    directory — ``render.render_artifact_dir`` (issue #72: the per-render
    directory the worker persists to INSIDE its
    ``tempfile.TemporaryDirectory`` with-block before the block exits;
    the directory survives past the tempdir teardown because the copy
    happened inside it).

    The durable path rides on the render object as a real attribute: the
    declared ``RenderResult`` field ``render_artifact_dir`` (the reader
    used to ``getattr`` a non-existent attribute name, which
    silently returned ``None`` on every real render — the bug this fix
    resolved). ``None`` for the field means "no durable source" — the
    frame omits the fields (never a bogus path, never a null, never a
    raise).

    The filenames are joined explicitly from the fixed VIEWS contract —
    never globbed: a directory that yields fewer than the 6 fixed VIEWS
    view PNGs (a failed copy, a missing file) is treated as "no views"
    (empty dict) — a consumer cannot distinguish a 5-of-6 map from a
    complete one, so the frame omits the field entirely rather than emit
    fewer than 6.
    """
    artifact_path = render.render_artifact_dir
    if not isinstance(artifact_path, str) or not artifact_path:
        return None, {}
    artifact_dir = Path(artifact_path)
    if not artifact_dir.is_dir():
        return None, {}
    stl_bytes = _read_artifact_bytes(str(artifact_dir / "model.stl"))
    view_bytes: dict[str, bytes] = {}
    for name, _cam in VIEWS:
        b = _read_artifact_bytes(str(artifact_dir / name))
        if b is not None:
            view_bytes[name] = b
    if len(view_bytes) != len(VIEWS):
        # Partial views → "no views" (the frame omits the field entirely).
        view_bytes = {}
    return stl_bytes, view_bytes


def _read_artifact_bytes(path: Any) -> bytes | None:
    """Raw bytes for a render artifact path, or ``None`` when unreadable
    (missing file, non-string path, or an ``OSError`` mid-read).
    """
    if not isinstance(path, str) or not path:
        return None
    p = Path(path)
    if not p.is_file():
        return None
    try:
        return p.read_bytes()
    except OSError:
        logger.exception("render artifact unreadable: %r", path)
        return None


def _suppress_cancellation(task: Any) -> None:
    """Best-effort cancel of a background task, swallowing the outcome.

    Deadline-triggered teardown (issue #221) cancels the ``to_thread``
    render task and the frame-queue consumer task; neither may leak an
    unhandled cancellation (a ``CancelledError`` that escapes ``.cancel()``
    would surface as an ``Exception ignored in Task`` warning — the client
    has already received the terminal frame, so the background work is
    unobservable by design: asyncio CANNOT kill a ``to_thread`` worker
    thread mid-flight, the terminal frame is the user-facing guarantee).
    """
    task.cancel()

    def _swallow_outcome(_t: Any) -> None:
        # ``to_thread`` tasks cannot be cancelled mid-flight — the worker
        # thread runs to completion (or until its own ``Event`` is
        # released). Swallow the eventual outcome so no unhandled
        # exception surfaces on the (closing) loop.
        #
        # On a task that was ``cancel()``-ed and has since reported
        # ``cancelled()`` True, ``.exception()`` is never reached — there
        # is no outcome to swallow. ``.exception()`` is only called when
        # ``cancelled()`` is False, i.e. the task RAN and raised: a
        # deadline-cancelled ``to_thread`` task that had already begun
        # propagating its cancellation can report ``cancelled()`` False
        # while still carrying a pending exception, and ``.exception()``
        # then RE-RAISES that exception from the done callback. That is
        # the only path that can raise here, so it is caught and logged
        # at debug level (the client already received the terminal frame;
        # the background outcome is unobservable by design — asyncio
        # CANNOT kill a ``to_thread`` worker thread mid-flight). Catching
        # the base exception type is deliberate: a ``CancelledError`` that
        # escapes a done callback would surface as an "Exception in
        # callback" traceback against a half-closed loop (measured: the
        # reproduction hangs the interpreter for the full ``asyncio.run``
        # shutdown timeout), so every outcome class must be swallowed.
        try:
            if not _t.cancelled():
                _t.exception()
        except BaseException:  # noqa: BLE001 — documented above
            logger.debug(
                "design-loop deadline teardown: swallowed a background "
                "task outcome that must not surface post-terminal-frame: %r",
                _t,
            )

    task.add_done_callback(_swallow_outcome)


def _result_message(result: Any) -> str:
    """The terminal ``done``/``error`` frame text for a loop result."""
    if getattr(result, "status", None) == "pass":
        return "Design loop passed validation"
    reason = getattr(result, "failure_reason", None)
    if isinstance(reason, str) and reason:
        return f"Design loop exhausted: {reason}"
    return "Design loop exhausted"


def _structured_reason(result: Any) -> str | None:
    """The loop's ``DesignResult.failure_reason`` as a plain string, or
    ``None`` — the SPA maps it to plain-language copy WITHOUT string-matching
    the free-text message (issue #82). The value is one of the
    ``GATE_REASON_BITS``, a render-worker ``ErrorClass`` (syntax_error,
    empty_model, artifact_error, timeout, oom, container_error), or a
    loop-level reason (``renderer_unavailable`` — the renderer pre-flight
    failed before any LLM call, issue #277; ``model_unconfigured`` — the
    model pre-flight found the LLM model unusable before any LLM call,
    issue #303; ``renderer_image_stale`` — the render-worker image pre-
    flight verified the image missing/stale before any LLM call, issue
    #346); ``None`` (absent from the frame) is the "no reason" case.
    """
    reason = getattr(result, "failure_reason", None)
    if isinstance(reason, str) and reason:
        return reason
    return None


def _axis_mismatches(result: Any) -> list[dict[str, Any]] | None:
    """The STRUCTURED per-param mismatches for an ``axis_params_mismatch``
    exhaustion (issue #276) — or ``None`` when the failure is a different
    reason (the field is then OMITTED, the frame's omit-not-null policy).

    Each entry is ``{"label": str, "model": float, "measured": float,
    "axis": str}`` — the same per-param evidence the repair directive
    carried to the loop's next iteration (``_evidence`` in
    ``run_design_loop_async``, built from the shared
    ``_axis_param_mismatches`` helper: every mismatching param, both
    numbers, no PII). The terminal error frame rides them in
    ``mismatches`` so the SPA's failure turn renders one line per
    mismatch via ``copy.failure.axisMismatchLine(label, mm(model),
    mm(measured))`` — the SPA owns the formatting; the numbers are the
    server's own gate numbers, never invented by the SPA.

    Derived from the BEST iteration's record (the same candidate the
    loop returned), not re-run through the gate: a stub loop result
    without the fields simply yields ``None`` (an honest absence, the
    #91/#137 precedent). The evidence string is the "; "-join of one
    line per mismatch; it is split back into entries by the known line
    shape (``"{label} = {model:g} but the part measures {measured:g}
    on {axis}"``) — malformed entries are dropped, never rendered as
    numbers the SPA has not established.
    """
    reason = getattr(result, "failure_reason", None)
    if reason != "axis_params_mismatch":
        return None
    best = getattr(result, "best", None)
    repair = getattr(best, "repair", None)
    if not isinstance(repair, dict):
        return None
    if repair.get("failure_class") != "axis_params_mismatch":
        return None
    evidence = repair.get("evidence")
    if not isinstance(evidence, str) or not evidence:
        return None
    # One line per mismatching param (the loop's own builder). Split the
    # "; "-join back into entries by the known line shape.
    pattern = re.compile(
        r"^(?P<label>.+?) = (?P<model>\d+(?:\.\d+)?(?:[eE][+-]?\d+)?) "
        r"but the part measures (?P<measured>\d+(?:\.\d+)?(?:[eE][+-]?\d+)?) "
        r"on (?P<axis>W|D|H)$"
    )
    out: list[dict[str, Any]] = []
    for part in evidence.split(";"):
        line = part.strip()
        if not line:
            continue
        m = pattern.match(line)
        if m is None:
            continue
        out.append(
            {
                "label": m.group("label"),
                "model": float(m.group("model")),
                "measured": float(m.group("measured")),
                "axis": m.group("axis"),
            }
        )
    return out or None


def _positive_axis_map(gate_axes: Any) -> dict[str, float] | None:
    """Normalise the gate's confirmed per-axis set to ``{str(axis): float}``
    (positive, finite, bool-excluded), or ``None`` when it is not a
    non-empty dict of usable entries — the shared coercion both
    ``_carried_axes`` and ``_measured_axes`` apply (identical rule, one
    definition)."""
    if not isinstance(gate_axes, dict) or not gate_axes:
        return None
    out: dict[str, float] = {}
    for axis, value in gate_axes.items():
        if isinstance(value, bool):  # bool is a subclass of int — exclude
            continue
        try:
            f = float(value)
        except (TypeError, ValueError):
            continue
        if f > 0:
            out[str(axis)] = f
    return out or None


def _carried_axes(result: Any, gate_axes: Any) -> dict[str, float] | None:
    """The axes the bbox gate ENFORCED on this turn (the caller's
    per-axis set, ``{"H": 12.0, ...}`` — the carried-plus-cued effective
    set, never just "cued this turn"), or ``None`` when the failing gate
    is not the bbox gate (the field is then omitted — omit-not-null, the
    frame policy).

    Issue #261 fix batch: with ``carried_axes`` on the terminal error
    frame, the SPA's failure copy can distinguish "the axis the user
    stated earlier was held, and the candidate missed it" (carried) from
    a value cued this turn — e.g. "raise it" enforces the carried H and
    the failure turn now names the held value instead of the generic
    "came out a different size" sentence.
    """
    reason = getattr(result, "failure_reason", None)
    if reason != "bbox_out_of_tolerance":
        return None
    return _positive_axis_map(gate_axes)


def _measured_axes(
    result: Any,
    gate_axes: Any,
    part_bbox_mm: tuple[float, float, float] | None = None,
) -> dict[str, float] | None:
    """The extents the bbox gate ACTUALLY compared on this turn (issue
    #367: the terminal error frame's ``measured_axes`` — the made values
    the SPA renders beside the user's asked values in the size-mismatch
    card), or ``None`` when the field is omitted (omit-not-null, the
    frame policy).

    Mirrors the gate's OWN selection (issue #389: the selection is now
    shared with the gate via :func:`d33d.design_loop._gate_selection_` ``extents``):
    a user-confirmed axis → the gate's own shape (a FULL positive
    (W, D, H) target with a component breakdown → the BEST-MATCHING
    component's extents, issue #100; a PARTIAL target → the whole-mesh
    extents); a floor-only target (issue #383 part-baseline, no user
    axis confirmed) → the whole-mesh extents; neither → ``None`` (the
    gate abstained — this field is omitted).

    Omitted (``None``) when: the failing gate is not the bbox gate; the
    best candidate carries no ``BboxInfo`` (the pre-flight placeholder);
    or the gate compared nothing (no axis confirmed — the gate abstained,
    so it cannot fail here either) or the compared extents are
    non-positive (a zero is the encoded absence, issue #91 — it is never
    emitted as a measured number).

    Import projects (issue #332, extended by issue #383), exactly:

    - PURE import (empty stated set): issue #383's part-baseline floor
      means the gate NO LONGER abstains — ``_bbox_target`` fills every
      unconfirmed axis with the part's own extent, so the gate compares
      the whole-mesh extents against the part and a shrunken candidate
      can genuinely fail on it. The field then carries the whole-mesh
      extents (the gate compared the part's extents); ``carried_axes``
      is empty (the user asked for nothing) — the field is OMITTED only
      when there is no failing bbox gate, no bbox, or the gate truly
      abstained (no part, no confirmed axis).
    - MIXED import (the user stated SOME axes): the partial confirmed
      triple makes ``gate_comparison_extents`` compare the WHOLE-MESH
      extents, and this field carries that made (W, D, H) — with
      ``carried_axes`` holding only the user's ask.
    """
    reason = getattr(result, "failure_reason", None)
    if reason != "bbox_out_of_tolerance":
        return None
    best = getattr(result, "best", None)
    if best is None:
        return None
    bbox = getattr(best, "bbox", None)
    if not isinstance(bbox, BboxInfo):
        return None
    # Normalise the user-confirmed set to a zero-filled (W, D, H) triple —
    # the gate's own input shape (``_bbox_within_tolerance``'s contract).
    # ``_bbox_target`` (the single shared definition) resolves the
    # import-project target; the field carries the extents the gate
    # actually compared, never the user's (empty) ask.
    confirmed = _positive_axis_map(gate_axes)
    triple = (
        (
            (confirmed.get("W", 0.0), confirmed.get("D", 0.0), confirmed.get("H", 0.0))
            if confirmed is not None
            else (0.0, 0.0, 0.0)
        ),
    )[0]
    target = _bbox_target(triple, part_bbox_mm)
    # The gate's own selection, via the shared helper (issue #389):
    # a user-confirmed axis → the gate's own shape (component vs whole
    # mesh); a floor-only target (issue #383) → the whole-mesh extents;
    # neither → None (the gate abstained — omit, never emit a vacuous
    # measurement).
    extents = _gate_selection_extents(bbox, triple, target)
    if extents is None:
        # No axis confirmed AND no floor (the gate abstained — it cannot
        # fail here either): omit, never emit a vacuous measurement.
        return None
    if any(e <= 0 for e in extents):
        return None
    return {"W": float(extents[0]), "D": float(extents[1]), "H": float(extents[2])}


def _loop_takes_app(run_loop: Any) -> bool:
    """The injected design-loop seam's signature check (production
    ``_build_production_design_loop`` takes ``(app, **kwargs)``; test
    stubs may take a subset — but the kwargs are ALWAYS forwarded,
    so a stub that consumes any of them (``on_progress`` in particular)
    must accept ``**kwargs`` or an ``app`` parameter; the
    app-parameter check only selects WHICH calling convention — see the
    call site in :func:`run_design_loop_with_events`)."""
    try:
        sig = inspect.signature(run_loop)
    except (TypeError, ValueError):
        return False
    return "app" in sig.parameters


def _run_in_loop(coro: Any) -> Any:
    """Drive ``coro`` to completion on a worker thread (``asyncio.run``).

    ``asyncio.to_thread`` schedules this function on the default executor (a
    worker thread with NO running event loop), so it ``asyncio.run``-s
    ``coro`` on a FRESH event loop. ``asyncio.run`` is required (not a bare
    ``await``) because the worker thread has no running loop to await
    against; ``asyncio.run`` installs that fresh loop and drives ``coro``.

    This is the spec's ``asyncio.to_thread`` requirement for the sync render:
    the multi-minute ``render_for_design_loop`` (Docker ``subprocess.run``)
    runs on the worker thread, NOT the app's event loop, so one slow render
    cannot stall other SSE streams or API handlers. The LLM's awaits run on
    the fresh loop (they still yield — the LLM is async) and do not block
    the app's event loop either, because the entire design loop runs here,
    off the app loop.

    ``coro`` MUST be the result of calling the injected loop (the adapter
    calls ``run_loop(...)`` before invoking this, so the loop's body runs
    here, not on the event loop). Exceptions from ``coro`` propagate out of
    ``asyncio.run`` and are caught by :func:`run_design_loop_with_events`'
    terminal-frame handler.
    """
    return asyncio.run(coro)


def _version_bbox_extents(result: Any) -> tuple[float, float, float] | None:
    """The best candidate's per-axis measured extents for persistence
    (issue #137), or ``None``.

    The version OWNS its measurement: the render that becomes the version
    is the BEST candidate (``result.best`` — the loop's best-scoring
    candidate, whose STL is the version's geometry), and the measurement
    comes from the ``BboxInfo`` that ``bbox_fn`` produced for THAT render
    (read from the best candidate's DECLARED ``IterationRecord.bbox``
    field — issue #93's precedent, the value ``bbox_fn`` returned for
    that render — never re-derived from the STL file, which may be gone
    by the time the version row is written).

    The persisted bbox is ALWAYS the WHOLE-MESH extents
    (``BboxInfo.x``/``y``/``z`` — the union bounding box of everything the
    render produced) whenever a valid ``BboxInfo`` exists with all three
    extents ``> 0`` (issue #247). This applies on BOTH the chat path and
    the finalize path (both funnel through this helper, via
    ``_resolve_version_create`` and ``d33d.versions_routes`` respectively),
    and is INDEPENDENT of stated dims, parameter names, and component
    count: reading the extents of a mesh is a measurement, not a guess —
    it does not need to know what the user asked for (DECISIONS.md 2026-
    09-24: abstention is right for choosing WHICH component the user
    meant, wrong for MEASURING).

    The pre-#247 rule is removed: when ``components`` was non-empty the
    helper matched the stated triple (read from the version's own params
    snapshot under the literal ``W``/``D``/``H`` keys) against the best-
    matching component and persisted that component's extents — or
    nothing. But ``bbox_from_render`` sets ``components`` for EVERY
    watertight body (a healthy single body splits to exactly one
    component), and the design model never emits ``W``/``D``/``H`` param
    keys (it emits free names like ``spacer_width``), so the branch
    always returned ``None``: versions.bbox was NULL on both the chat and
    the finalize path for every real design. Component matching is no
    longer used for persistence at all.

    A zero extent or a missing ``BboxInfo`` still persists ``None`` (the
    row stores NULL — honest absence): a zero is the encoded absence
    (issue #91), and an absent measurement abstains (issue #137) — it is
    never encoded as ``(0, 0, 0)``.
    """
    best = getattr(result, "best", None)
    if best is None:
        return None
    bbox = getattr(best, "bbox", None)
    if not isinstance(bbox, BboxInfo):
        return None
    if any(e <= 0 for e in (bbox.x, bbox.y, bbox.z)):
        return None
    return (bbox.x, bbox.y, bbox.z)


def _version_confirm_hints(result: Any) -> tuple[str | None, str | None]:
    """The best candidate's offer hints for the adapter (issue #250), or
    ``(None, None)``.

    The hints are DECLARED ``IterationRecord`` fields (``confirm_first`` /
    ``confirm_sentence`` — set from the design-role reply by
    ``d33d.design_loop.extract_confirm_hints``); read from the record, never
    re-derived. A stub result without the fields (an older loop seam) or a
    model that emitted no hints both yield ``(None, None)`` — the offer
    selection still has the declared-axis branch to run.
    """
    best = getattr(result, "best", None)
    if best is None:
        return None, None
    first = getattr(best, "confirm_first", None)
    if not isinstance(first, str) or not first.strip():
        first = None
    sentence = getattr(best, "confirm_sentence", None)
    if not isinstance(sentence, str) or not sentence.strip():
        sentence = None
    return first, sentence


def _value_matches_quoted(entry: dict[str, Any], quoted: set[float]) -> bool:
    """True iff the entry's value is a number (not bool) and equals any
    of the quoted values within 1e-6 tolerance (issue #261 tier 2)."""
    value = entry.get("value")
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    return any(abs(float(value) - n) <= 1e-6 for n in quoted)


async def _resolve_offer(
    app: Any,
    project_id: int,
    version_id: int | None,
    result: Any,
    prev_version: Any,
    prev_confirmed: dict[str, Any] | None = None,
    user_message: str = "",
    chat_history: tuple[str, ...] = (),
) -> dict[str, Any] | None:
    """Resolve the pending offer for a passing design pass (issue #250).

    On a pass (``version_id`` set): pick AT MOST ONE assumed param of the
    NEW version, EXCLUDING any param the caller passes in
    ``prev_confirmed`` (the pre-pass latest version's ``confirmed_params``
    — the CALLER reads it and passes it explicitly, per project, so
    interleaved passes on different projects can never read each other's
    confirmed set) and any param the user changed (the diff vs
    ``prev_version``'s params — a param already confirmed or user-changed
    is never re-offered), then build the sentence (the model's
    ``confirm_sentence`` only when it names the chosen value AND passes
    the issue #249 number guard against the NEW version's own design-state
    block — the new row's ``confirmed_params``, never the pre-pass set —
    else the deterministic template), and persist the offer server-side
    (``versions.set_pending_offer`` — the acceptance check in
    ``post_chat`` reads it, never the client). Returns the done frame's
    additive ``offer`` field ``{"param", "sentence"}``.

    No confirmed-set carry-forward is written: the NEW version row's
    ``confirmed_params`` stays NULL until the user accepts THAT
    version's offer — a confirmation the user made on a PREVIOUS version
    is still honoured on the new row by the selection (``prev_confirmed``
    excludes a carried param from being re-offered) and by rule (b) (the
    design-state block re-checks the value against the row's own set, which
    is empty until the user accepts this version's offer — a value change
    in this version breaks the chain by construction).

    ``None`` in every no-offer case (no new version, no eligible param,
    offer state write failed — logged, never fatal).
    """
    if version_id is None:
        return None
    from d33d.confirm_offer import (
        offer_entry,
        offer_sentence,
        select_offer_candidate,
    )
    from d33d.design_state import state_block_for_version

    versions = app.state.versions
    new_version = versions.get_version(project_id, version_id)
    if new_version is None:
        return None
    params: dict[str, Any] = dict(new_version["params"] or {})
    meta = new_version["param_meta"]
    # The NEW version's FULL design-state block (issue #264): computed
    # ONCE here — it feeds BOTH the offer-eligibility exclusion set and
    # the tier-3 number guard (the ``block`` the sentence checks
    # against). The block is the only place the measurement comparison
    # runs, so it is also the single source of the disagree set below.
    block = state_block_for_version(
        params,
        new_version["bbox"],
        new_version["stated_dims"],
        meta,
        new_version["confirmed_params"],
    )
    # The single-source seam (issue #300): the block's PARAM rows — the
    # SAME rows the Brief shows — feed the selection, the entry lookup
    # and the sentence guard; the block is built ONCE (above) and is
    # never recomputed per tier, per param, or per ``offer_entry`` call
    # (``state_block_from_params`` must not run again on the selection
    # path). Only param rows (``kind == "param"``) enter any set below:
    # an axis row is never an offer target, and its name never enters an
    # exclusion set (offers are params, not axes).
    param_rows = [e for e in block if e.get("kind") == "param"]
    # Issue #264 — offer eligibility must exclude any param the block
    # renders ``disagrees`` (either source — user-stated or
    # model-emitted): a value the measurement contradicts is never an
    # assumption to confirm (offering it would ask the user to affirm a
    # number the part itself disproves). The set is passed EXPLICITLY to
    # ``select_offer_candidate`` (the block rows above already carry the
    # same provenance — the explicit set stays, so the exclusion is
    # always explicit, never inferred).
    disagree_names = {
        e["name"] for e in param_rows if e.get("provenance") == "disagrees"
    }
    # The confirmed set for SELECTION is the pre-pass latest version's
    # ``confirmed_params`` (the caller's ``prev_confirmed`` — read and
    # passed explicitly, never stashed in ``app.state`` where an
    # interleaved pass on another project could read it): a param the
    # user confirmed ON A PREVIOUS version is confirmed evidence, not an
    # assumption to re-confirm — a carried forward confirmed param
    # (identical value) is excluded from the new version's offer.
    confirmed = prev_confirmed
    # The user's changed set vs the previous version (issue #250: a param
    # the user changed via the design loop — a param the user asked for
    # and whose value now differs from the previous version — is never
    # re-offered; it is the user's own move, not an assumption to confirm).
    # ONLY value changes count: a param newly introduced this version is
    # NOT in the previous snapshot at all, so it is not "changed" — it is
    # a fresh assumption and is offerable. On the project's FIRST version
    # (no previous version) the changed set is empty by definition.
    prev_params: dict[str, Any] = dict(prev_version["params"]) if prev_version else {}
    changed = {
        name
        for name, value in params.items()
        if name in prev_params and prev_params[name] != value
    }
    confirm_first, confirm_sentence_raw = _version_confirm_hints(result)
    # Issue #261's offer tiering — the two signals from ONE helper
    # (``offer_tier_signals`` — the offer's seam): TIER 1 (a released-
    # axis param) needs THIS turn's lexicon classification of the user's
    # message (relative cues release their axis, global cues release
    # all); TIER 2 (a user-quoted unmapped number) needs the recent-
    # history scan over (*chat_history, user_message). The finalize seam
    # passes the project's chat history here too, so tier 2 behaves the
    # same on finalize as on chat (a number quoted in an EARLIER message
    # is eligible, not only when it sits in the finalize message).
    from d33d.dimension_protocol import offer_tier_signals

    released_axes, quoted = offer_tier_signals(user_message, chat_history)
    # The tier-1 honesty gate (issue #312, task-b): the "You asked for
    # {cue} — I made {label} {value}. Right?" sentence claims a CHANGE,
    # so a released-axis param whose value is UNCHANGED is not a tier-1
    # candidate. The baseline, per axis (the operator decision): the
    # previous version's param value when the previous version declared
    # that param; else the carried stated value for the axis — task-a's
    # project-level ``carried_stated_dims`` set when the column exists
    # (the user's last statement, which survives a failed turn even when
    # the previous version row is absent), else the previous version's
    # persisted ``stated_dims`` (the pre-task-a fallback); else no
    # baseline (nothing to compare against is not a lie — the tier
    # applies). A candidate at its baseline value keeps the param
    # eligible for EVERY OTHER tier (the exclusion is tier-level, not
    # candidate-level — the param is still an assumption worth
    # confirming); only the "I made" sentence is withheld: tier 1
    # selection skips it (``select_offer_candidate`` with the candidate
    # re-scoped off tier 1), and the sentence dispatch below falls
    # through to the next applicable tier for it.
    tier1_eligible: set[str] = set()
    tier1_blocked: set[str] = set()
    if released_axes:
        carried_set: dict[str, float] = {}
        # The carried stated value (task-a's project-level set, when the
        # column exists — it survives a failed turn where no version row
        # was created), else the previous version's persisted
        # ``stated_dims`` (the pre-task-a fallback).
        if prev_version is not None:
            row = app.state.conn.get_project(project_id)
            raw_carried = row.get("carried_stated_dims") if row else None
            if isinstance(raw_carried, str) and raw_carried:
                try:
                    loaded = json.loads(raw_carried)
                    if isinstance(loaded, dict):
                        carried_set = loaded
                except ValueError:
                    carried_set = {}
            elif isinstance(raw_carried, dict):
                carried_set = raw_carried
            if not carried_set and isinstance(
                prev_version.get("stated_dims"), dict
            ):
                carried_set = prev_version["stated_dims"]
        for e in param_rows:
            axis = e.get("axis")
            if axis not in released_axes:
                continue
            value = e.get("value")
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                continue
            prev_value = prev_params.get(e["name"])
            baseline: float | None = None
            if isinstance(prev_value, (int, float)) and not isinstance(
                prev_value, bool
            ):
                baseline = float(prev_value)
            else:
                raw = carried_set.get(axis)
                if isinstance(raw, (int, float)) and not isinstance(raw, bool):
                    baseline = float(raw)
            if baseline is None:
                continue
            if abs(float(value) - baseline) <= 1e-6:
                tier1_blocked.add(e["name"])
            else:
                tier1_eligible.add(e["name"])
    # Tier 1 selects over the released-axis candidates EXCEPT the
    # unchanged ones (tiers 2 / 3 still see the full eligible set — the
    # candidate itself was never excluded, only its "I made" tier; this
    # re-scoping only prevents tier 1 from CHOOSING an unchanged param,
    # so the selection order is preserved for the honest candidates).
    tier1_axes = released_axes
    if tier1_blocked:
        tier1_axes = {
            e.get("axis")
            for e in param_rows
            if e.get("axis") in released_axes and e["name"] in tier1_eligible
        }
    name = select_offer_candidate(
        params, meta, confirmed, changed, confirm_first,
        released_axes=tier1_axes or None, user_quoted_mm=quoted,
        disagree_names=disagree_names, block_entries=param_rows,
    )
    if name is None:
        versions.set_pending_offer(project_id, None)
        return None
    # The chosen param's entry comes from the SAME block (the single-
    # source seam — the label + value the Brief shows, with the same
    # ``meta_unit`` / ``param_axis`` graft), never a second build.
    entry = offer_entry(params, meta, name, block_entries=param_rows)
    if entry is None:
        versions.set_pending_offer(project_id, None)
        return None
    # The sentence: the tier that WON picks its template (issue #261 —
    # tier 1 "You asked for {cue}…", tier 2 "You said {value}…"); tier 3
    # keeps the #250 machinery (the model's ``confirm_sentence`` when the
    # number guard passes, else the deterministic template).
    # Issue #312's tier-1 honesty gate: a released-axis param whose value
    # equals its baseline (previous version's value, else the carried
    # stated value) gets NO "I made" — the offer still stands (the other
    # tiers above selected it); the sentence falls through to the next
    # tier's machinery (the model's sentence when the number guard
    # passes, else the deterministic "I assumed …" template — which
    # claims nothing).
    tier1_ok = bool(
        released_axes
        and entry.get("axis") in released_axes
        and entry.get("name") in tier1_eligible
    )
    if tier1_ok:
        from d33d.confirm_offer import tier_1_cue, tier_1_sentence

        cue = tier_1_cue(user_message)
        if cue is None:
            sentence = offer_sentence(entry, confirm_sentence_raw, block)
        else:
            sentence = tier_1_sentence(entry, cue)
    elif quoted and _value_matches_quoted(entry, quoted):
        from d33d.confirm_offer import tier_2_sentence

        sentence = tier_2_sentence(entry)
    else:
        sentence = offer_sentence(entry, confirm_sentence_raw, block)
    versions.set_pending_offer(project_id, {"version_id": version_id, "param": name})
    return {"param": name, "sentence": sentence}


def _inherit_param_labels(
    meta: dict[str, Any], prev_meta: Any
) -> dict[str, Any]:
    """Inherit parameter labels from the previous version's metadata
    (issue #279, task-b): where the previous version declared a label
    for a parameter that also appears in the new version's metadata, the
    PREVIOUS label is kept — a regeneration that renames "Overall height"
    to "Height" must not move or relabel the row under the user's eye.

    Match, in strict precedence (name first, axis only as a fallback):

    1. NAME MATCH — the new param's name was a key in the previous
       metadata; inherit that entry's label. This covers the common
       case (the model keeps the param name, renames the label) and the
       rename-the-NAME case when the axis is declared on one side only.
    2. AXIS MATCH — the new param name is NEW to the previous version, it
       declares an axis, EXACTLY ONE previous param declared that axis and
       has a label, and EXACTLY ONE new param with that axis has no name
       match (issue #279's operator tie-break — a rename of the param name
       with the axis unchanged inherits through the axis; AMBIGUOUS (several
       previous or several new params share the axis) does not inherit — the
       new label stands). A name-matched previous param on the axis DOES
       make the axis ambiguous for the OTHER new params (the axis inventory
       is the FULL previous inventory — the same tie-break that the binding
       applies), and a label-less previous param on the axis is not a
       source (the new label stands, never a fabricated or ``None`` one).

    ONLY the ``label`` field is inherited: ``unit`` / ``axis`` / ``reason``
    always come from the NEW metadata (the operator decision — a rename
    keeps the label, the rest is the new version's own declaration). The
    previous label is kept UNCONDITIONALLY: a user rename in chat is
    explicitly out of scope, so the previous label wins even then.

    Degraded inputs never fail: a ``None`` / non-dict previous metadata
    (no previous version, or a version without metadata) inherits nothing;
    a previous entry without a usable string label leaves the new label
    as-is — including on the axis path (a label-less predecessor is not a
    source, so the new label stands, never a ``KeyError`` or a ``None``);
    a new entry with no label of its own receives the inherited one when a
    labelled match exists. The result is the new metadata with labels
    grafted — never a mutated input, never a fabricated label.
    """
    from d33d.design_state import normalize_param_meta

    if not isinstance(prev_meta, dict) or not prev_meta:
        return dict(meta)
    prev = normalize_param_meta(prev_meta)
    prev_labels = {
        name: m["label"] for name, m in prev.items() if m.get("label")
    }
    if not prev_labels:
        return dict(meta)
    # Axis inventory of the FULL previous version (including name-matched
    # prev params — the binding tie-break: a name-matched prev entry on the
    # same axis makes the axis ambiguous for the OTHER new params; the
    # axis pass only looks at entries that also carry a label).
    prev_axis_names: dict[str, list[str]] = {}
    for name, m in prev.items():
        axis = m.get("axis")
        if axis:
            prev_axis_names.setdefault(axis, []).append(name)
    # Name matches: a new param whose name was in the previous version
    # inherits that param's label (when the previous one had one).
    matched: dict[str, str] = {}
    for name in meta:
        if name in prev_labels:
            matched[name] = prev_labels[name]
    # Axis inventory of the NEW version among the UN-matched new params
    # (precomputed once after the name pass — the axis pass is two length
    # lookups, not a re-scan).
    new_axis_names: dict[str, list[str]] = {}
    for name, m in meta.items():
        if name in matched or not isinstance(m, dict):
            continue
        axis = m.get("axis")
        if axis:
            new_axis_names.setdefault(axis, []).append(name)
    # Axis matches: a new param that did NOT name-match, declares an
    # axis, and is the ONLY un-matched new param with that axis inherits
    # from the previous version's ONLY LABELED param with that axis (the
    # operator tie-break — ambiguity inherits nothing). The inventory is
    # the FULL previous axis inventory — including name-matched prev
    # params (a name-matched prev entry on the same axis makes the axis
    # ambiguous for the other new params, per the tie-break), so a
    # label-less unique predecessor is not a source and the new label
    # stands.
    for name, m in meta.items():
        if name in matched:
            continue
        axis = m.get("axis") if isinstance(m, dict) else None
        if not axis:
            continue
        if len(new_axis_names.get(axis, [])) != 1:
            continue
        prev_with_axis = [n for n in prev_axis_names.get(axis, []) if n in prev_labels]
        if len(prev_with_axis) != 1:
            continue
        matched[name] = prev_labels[prev_with_axis[0]]
    # Graft the inherited labels onto a COPY of the new metadata (the
    # inputs are never mutated): a matched entry keeps its own
    # unit/axis/reason, receives the previous ``label``, and is stored
    # back as a fresh dict. An unmatched entry is copied through
    # verbatim (a non-dict entry — a malformed row — keeps its shape; a
    # dict entry is copied so a later caller never sees a shared
    # reference into the loop record's metadata).
    out: dict[str, Any] = {}
    for name, entry in meta.items():
        if isinstance(entry, dict):
            entry = dict(entry)
        if name in matched:
            if isinstance(entry, dict):
                entry["label"] = matched[name]
            else:
                entry = {"label": matched[name]}
        out[name] = entry
    return out


def _version_param_meta(
    result: Any, prev_meta: Any = None
) -> dict[str, Any] | None:
    """The best candidate's per-parameter metadata for persistence
    (issue #248), with labels inherited from the previous version's
    metadata (issue #279, task-b), or ``None``.

    The version OWNS its metadata: the render that becomes the version is
    the BEST candidate (``result.best``), and the metadata is that
    candidate's DECLARED ``IterationRecord.param_meta`` field (issue
    #248's precedent — read from the record, never re-derived from the
    LLM result). ``{}`` (a model that emitted no ``parameters`` array)
    and a missing field both persist ``None`` (the row stores NULL — an
    honest absence; an empty dict is not a metadata set, never a
    fabricated label).

    ``prev_meta`` (the previous version's row ``param_meta`` — ``None``
    when there is no previous version or the row carries no metadata) is
    the label-inheritance source: where it declares a label for the same
    param (by name, or by a unique axis match for a renamed param), that
    label is kept so a regeneration's relabel does not move the row
    (issue #279's operator decision). The rest of each entry — unit,
    axis, reason — is never inherited: the new version declares its own.
    """
    best = getattr(result, "best", None)
    if best is None:
        return None
    meta = getattr(best, "param_meta", None)
    if not isinstance(meta, dict) or not meta:
        return None
    return _inherit_param_labels(meta, prev_meta)


def _version_render_artifact_dir(result: Any) -> str | None:
    """The durable on-disk path of the render that produced the version
    (issue #163), or ``None``.

    The version OWNS its render reference: the render that becomes the
    version is the BEST candidate (``result.best`` — the loop's
    best-scoring candidate, whose ``model.stl`` is the version's geometry),
    and the reference is that render's DECLARED
    ``RenderResult.render_artifact_dir`` field (issue #72's durable
    per-render directory, ``<renders_dir>/<uuid8>``) — the same value the
    SSE adapter reads for the artifact bytes. Renders land under a fresh
    per-render uuid unrelated to version ids, so the link is PERSISTED at
    version-creation time and never re-derived (the 3MF download route
    cannot infer it from mtime — a "probably right" directory is exactly
    what issue #163's spec rejects). ``None`` (a stub loop result without
    the field, or a render without a durable directory) stores a NULL:
    an absent render record degrades honestly at the route (a clear
    non-2xx), never a guess.
    """
    best = getattr(result, "best", None)
    if best is None:
        return None
    render = getattr(best, "render", None)
    if render is None:
        return None
    artifact_path = getattr(render, "render_artifact_dir", None)
    if not isinstance(artifact_path, str) or not artifact_path:
        return None
    return artifact_path


def _stored_part_mesh_path(row: dict[str, Any], conn: Any) -> Path | None:
    """Issue #386 (final): the stored, REPAIRED part mesh path the design
    loop's render imports — ``{repo}/versions/{v1}/part.stl`` (STL
    imports).

    The "3MF has no STL to measure" knowledge lives here (issue #386
    final cleanup): ``None`` is returned whenever the resolved path's
    suffix is not ``.stl`` — a 3MF import is filtered at the source,
    so the caller needs no format string compare.

    ``None`` for a missing v1 row, missing repo path, no part, a
    non-STL part (a 3MF import), or a missing file on disk. The file
    is NOT read here — the caller measures it off the event loop.
    """
    from d33d.part_http import resolve_v1_part_path

    path, _repo_dir = resolve_v1_part_path(row, conn)
    if path is None:
        return None
    if path.suffix != ".stl":
        return None
    if not path.is_file():
        return None
    return path


def _measured_genus_for_file(stl_path: str) -> int | None:
    """Issue #386 (final): measure a stored part mesh's genus (the
    ``model.stl``-twin of :func:`_measured_genus_for_dir` for a part
    path).

    Returns ``None`` (the check abstains) when the file is missing,
    the load/split fails, or there are zero watertight components — a
    fabricated baseline would make the gate lie.
    """
    from d33d.part_mesh_topology import genus_from_stl

    return genus_from_stl(stl_path)


def _measured_genus_for_dir(render_artifact_dir: str) -> int | None:
    """Issue #386 (v2+ baseline): measure the parent version's rendered
    genus from its ``render_artifact_dir`` (the ``model.stl`` inside).

    Returns ``None`` (the check abstains) when the directory or the
    ``model.stl`` is missing, the load/split fails, or there are zero
    watertight components — a fabricated baseline would make the gate lie.
    """
    from d33d.part_mesh_topology import genus_from_stl

    stl_path = Path(render_artifact_dir) / "model.stl"
    if not stl_path.is_file():
        return None
    return genus_from_stl(str(stl_path))


async def _resolve_version_create(
    app: Any,
    project_id: int,
    result: Any,
    user_message: str,
    stated_axes: dict[str, float] | None = None,
) -> int | None:
    """On a ``pass``: create the version and return its id — a passing
    loop ALWAYS materialises a version (issue #93), with whatever params
    are known (possibly none).

    The version OWNS its geometry (issue #105): the passing best
    candidate's ``scad_source`` (a declared ``IterationRecord`` field —
    the value the loop's prompt builder already carried in the
    ``design_source`` prompt section on the loop's OWN iterations) is
    persisted as ``versions/{id}/design.scad`` inside
    ``create_version`` (same commit as the params snapshot). The loop's
    best candidate is authoritative for BOTH params and source — a stub
    without the declared field (or with an empty source) still versions
    params only (no spurious empty source file).
    Params, in strict precedence:

    1. the best candidate's OWN render parameters — read as a DECLARED
       ``IterationRecord`` field (issue #93: a duck-typed read of an
       attribute that was not a declared field used to return ``None`` for
       every real candidate, so a fresh project never got a version). A
       declared empty dict ({}) means "a dimensionless pass with no known
       parameters" and IS used as-is — it is the loop's authoritative
       answer, and an empty params set is legal for ``create_version``
       (``validate_params`` accepts it);
    2. else the latest version's params snapshot (a mid-project pass whose
       candidate somehow carries a non-dict param set — the fallback is
       unchanged in meaning from before the fix; ``latest_version`` is
       read once above for the name derivation and reused here);
    3. else ``{}`` — the version is still created: a pass with unknown
       dimensions is a legitimate state and the user must still get their
       model (the old guard returned ``None`` here, suppressing the
       version entirely).

    The version ``name` (issue #245) is derived from WHAT CHANGED, never
    from the raw user message (a question like "how tall is it now" used
    to become the version name). Name source, in strict precedence:

    1. the model's ``// title:`` leading comment in the candidate's own
       SCAD — extracted like params (``d33d.design_loop.scad_title``),
       cleaned via ``d33d.versions.clean_name`` (the loop's design prompt
       now asks the model for exactly this comment line);
    2. a deterministic phrase from the param diff vs the previous
       version's params (``d33d.versions.param_diff_name``):
       "First design" / "<name> <old> → <new>" / "<n> parameters
       changed" / "Revised geometry".

    BOTH name sources are cleaned through ``clean_name`` with the project's
    existing version names as the collision baseline (the #245 follow-up):
    ``VersionService`` passes an explicit name through verbatim, so without
    the suffix here, two passes that yield the same title or diff phrase
    would create two identically named versions — the second gets
    "<name>-2".

    The raw user message is still passed as the ``message`` field
    (provenance — the "triggering message excerpt" the timeline renders)
    and NEVER becomes the name. ``None`` is returned only when ``best``
    itself is missing (a loop result that does not carry a candidate at
    all — a contract violation that must not fabricate a version), never
    when the parameter set is merely empty. The version ``message` is the user's chat text truncated to
    200 characters (Python string slicing is code-point-safe — no
    multi-byte split, unlike a raw byte slice)."""
    best = getattr(result, "best", None)
    if best is None:
        return None
    # The previous version, read ONCE up front (a single-row query): it is
    # both the param-diff baseline for the name (issue #245) and the
    # params fallback for the non-dict-params case below.
    latest = app.state.versions.latest_version(project_id)
    named = best.params  # a declared IterationRecord field (issue #93)
    if not isinstance(named, dict):
        # The only reachable case for a real record: a defensive fallback
        # that cannot actually fire — kept because the adapter is typed
        # ``Any`` and a corrupt record (non-dict param set) must degrade
        # to the latest-version snapshot rather than fabricate one.
        named = dict(latest["params"]) if latest is not None else {}
    # The candidate's OWN source (a declared field — ``best.scad_source``
    # is verified against the real ``IterationRecord``, not a stub's
    # assumed shape; an empty string / non-str yields no source file).
    candidate_source = getattr(best, "scad_source", None)
    if not isinstance(candidate_source, str) or not candidate_source.strip():
        candidate_source = None
    # The version name (issue #245): WHAT CHANGED, never the raw message.
    # The model's ``// title:`` comment in the candidate's own SCAD wins
    # when present; otherwise a deterministic phrase from the param diff
    # vs the previous version's params. (``named`` is a dict at this
    # point — the non-dict fallback just ran.)
    prev_params: dict | None = dict(latest["params"]) if latest is not None else None
    # Lazy import: ``d33d.versions`` imports ``d33d.projects`` (which imports
    # this module), so the name helpers are pulled in at call time.
    from d33d.versions import resolve_version_name

    # The collision-suffix baseline (#245 follow-up): the set of names the
    # project already carries — ``VersionService`` passes an explicit name
    # through verbatim, so the suffix must be applied HERE, on BOTH the
    # title and the param-diff phrase, or two passes with the same diff
    # phrase ("2 parameters changed") would create two identically named
    # versions. A failure here must NEVER kill the version: the name may
    # lack its suffix, but the version is still created.
    try:
        existing_names = {v["name"] for v in app.state.versions.list_versions(project_id)}
    except Exception:
        logger.warning(
            "list_versions failed for project %s; creating the version "
            "without a collision baseline (the name may lack a suffix)",
            project_id,
            exc_info=True,
        )
        existing_names = set()

    # The version name (issue #276): the SHARED resolver (the finalize
    # route uses the same one, so both paths name identically). The chat
    # path passes no client name — the precedence falls straight to the
    # model's ``// title:`` comment (sanitised against the measured bbox,
    # ``None`` abstains), else the param-diff phrase.
    version_name = resolve_version_name(
        None,
        candidate_source,
        measured_bbox=_version_bbox_extents(result),
        prev_params=prev_params,
        new_params=dict(named),
        existing_names=existing_names,
    )
    # The thumbnail is the best render's iso view (the only view guaranteed
    # to frame the whole object — side views can be cropped per the
    # separately-tracked camera-fit issue). ``_artifact_bytes_from_path``
    # returns an empty dict for ``view_bytes`` when fewer than the full
    # 6-view set is present, so the pick degrades to ``None`` (a NULL row)
    # rather than substituting a different view or a placeholder. The name
    # IS passed (issue #245): the raw user message is the ``message``
    # field (provenance) and never becomes the name.
    render = getattr(best, "render", None)
    thumbnail = None
    if render is not None:
        _stl, view_bytes = _artifact_bytes_from_path(render)
        iso = view_bytes.get("view_05_iso.png")
        if iso is not None:
            thumbnail = _data_uri_from_bytes(iso, "image/png")
    version = await app.state.versions.create_version(
        project_id,
        dict(named),
        name=version_name,
        message=user_message[:200],
        scad_source=candidate_source,
        thumbnail=thumbnail,
        bbox=_version_bbox_extents(result),
        render_artifact_dir=_version_render_artifact_dir(result),
        stated_dims=stated_axes,
        param_meta=_version_param_meta(
            result, prev_meta=(latest.get("param_meta") if latest is not None else None)
        ),
    )
    return int(version["id"])




async def run_design_loop_with_events(
    app: Any,
    project_id: int,
    *,
    user_message: str,
    stated_dims: tuple[float, float, float] | None,
    chat_history: tuple[str, ...],
    photo: str,
    request_text: str,
    stated_axes: dict[str, float] | None = None,
) -> AsyncIterator[tuple[str, dict[str, Any]]]:
    """Run the injected design loop (``app.state.run_design_loop``) for
    one chat message and yield the SSE frame contract.

    The heavy render work (``render_fn=render_for_design_loop`` — Docker
    subprocess work, minutes) is run off the event loop per the spec's
    ``asyncio.to_thread`` requirement — see :func:`_run_in_loop` — so a
    slow render does not stall other SSE streams or API handlers. The
    adapter itself is an async generator that yields frames as the loop
    progresses. Every exit path (pass, exhausted, exception) ends in a
    terminal ``done``|``error`` frame — the client
    (``ApiClient.streamEvents``) resolves only on a terminal frame, so an
    unhandled exception here would leave the stream hanging.

    ``photo`` is the project's stored photo as a data URI (computed by the
    caller — the route — synchronously before the 202 response; the
    background task runs via asyncio and the DB may be closed by the time
    the loop starts).

    ``stated_dims`` may be ``None`` ("no dimensions known" — the /chat
    caller path, ticket #91). The adapter no longer substitutes
    ``(0.0, 0.0, 0.0)`` here: a fabricated zero triple is never a gate
    target.

    The bbox gate itself changed for ALL callers (ticket #91): it now
    ABSTAINS on an unknown (``<= 0``) target instead of hard-failing it,
    and records the abstention distinctly in ``d33d.design_loop.score``'s
    ``bbox_abstained`` field. This includes ``create_region_edit``, which
    still deliberately passes a ``(0.0, 0.0, 0.0)`` triple on a fresh
    project — its bbox bit flipped from FAIL to ABSTAIN (recorded as
    ``Score.bbox_abstained``). The abstain is the correct semantics for
    that route: its own comment says "the dimension gate measures rather
    than fabricates", which IS abstain semantics — the old hard-fail
    contradicted it.
    """
    # The per-view progress frames (``render-view-*``) land on this queue
    # as the drain thread enqueues them. The generator is the sole
    # consumer, so an ``asyncio.Queue`` needs no locking; frames are
    # yielded while the render is still running (see the ``asyncio.wait``
    # below), not after it completes.
    _frame_queue: asyncio.Queue[tuple[str, dict[str, Any]] | None] = asyncio.Queue()

    run_loop = getattr(app.state, "run_design_loop", None)
    if run_loop is None:
        yield ("error", {"message": "design loop not wired"})
        return

    # Issue #121: the per-view progress frames (``render-view-start`` /
    # ``render-view-done``) are produced by the render container's
    # stderr-drain thread (a worker thread, no event loop). The adapter
    # captures the current event loop HERE (before the loop is started —
    # the loop runs on a worker thread via ``asyncio.run`` and has no
    # loop to capture) and builds an ``on_progress`` hook that enqueues
    # a ``loop.call_soon_threadsafe`` schedule onto that loop via
    # ``run_coroutine_threadsafe``. The hook is sync (the drain thread
    # calls it directly); the async work (yielding the frame) happens on
    # the app's loop via the future the hook schedules. The stream
    # (this generator) is the sole consumer; no shared state is needed
    # because the generator is a single async consumer of one stream.
    #
    # The loop is started on ``asyncio.to_thread(_run_in_loop, raw)`` —
    # ``_run_in_loop`` runs ``asyncio.run(coro)`` on a worker thread, so
    # the render's ``subprocess.run`` (Docker) runs off the app's loop.
    # The drain thread is a child of that worker thread's ``subprocess
    # .run``; it must therefore schedule onto the APP's loop (captured
    # here), not the worker thread's fresh loop.
    def _on_progress(kind: str, payload: dict[str, Any]) -> None:
        """The drain thread calls this sync hook for each marker.

        ``kind`` is ``"view-start"`` or ``"view-done"`` (``view-failed``
        is filtered out — a failed view must not report progress).
        ``payload`` carries ``view`` (the view stem) and ``iteration``
        (the design-loop iteration index, 1-based).

        The hook schedules a ``put_nowait`` onto ``_frame_queue`` via
        ``_loop.call_soon_threadsafe`` (the hook runs on the drain's
        worker thread, so it must not touch the loop's queue from that
        thread); the generator yields the frame while the render is STILL
        running (see the ``await asyncio.wait`` below) — live, not
        batched at render completion.
        """
        if kind not in ("view-start", "view-done"):
            return
        view = payload.get("view", "")
        if not view:
            return
        step_name = (
            "render-view-start" if kind == "view-start" else "render-view-done"
        )
        _loop.call_soon_threadsafe(
            _frame_queue.put_nowait,
            ("progress", {"step": step_name, "view": view, "iteration": payload.get("iteration", 0)}),
        )

    yield ("progress", {"step": "design-loop-start"})

    # The full design-loop kwargs contract (the same shape the finalize
    # seam's ``_finalize_loop_kwargs`` builds — photo as a data URI,
    # stated_dims from the caller — ``None`` when no dimensions are known,
    # never a fabricated ``(0.0, 0.0, 0.0)``; the loop's bbox gate abstains
    # on an unknown triple and records it in ``Score.bbox_abstained`` —
    # ticket #91), a real bbox_fn, and the ``request`` guaranteed
    # non-empty so an exhausted loop still archives to failures.jsonl.
    # ``render_fn`` and ``llm_fn`` are ``None`` by contract: the production
    # closure (``_build_production_design_loop``) builds its OWN
    # ``render_fn`` (``render_for_design_loop``) and ``llm_fn`` (from the
    # live catalogue) and does not consume these kwargs; a future seam
    # variant that DOES consume them would need to supply real callables
    # (the ``None`` placeholders are not a fallback — see the production
    # seam's ``render_fn=render_for_design_loop`` hardcode).
    # ``attempt_timeout`` (issue #417) is the loop's per-attempt wall-clock
    # deadline — a REAL kwarg the loop consumes (``run_design_loop_async``
    # times each design LLM call against it and returns the best-so-far
    # candidate on expiry). The production closure forwards it like
    # every other kwarg; a stub seam that does not consume it simply
    # ignores it (the ``**kwargs`` contract).
    #
    # ``request`` carries the CURRENT user's message (``user_message`` —
    # issue #97: the message used to be forwarded only as the
    # failures.jsonl hook's archive field and was never rendered in the
    # design prompt, so the model never saw what was asked). It rides
    # the single ``request`` kwarg end to end: the hook pops a copy for
    # archiving AND forwards the value to the loop, where
    # ``_design_messages`` renders it as the first ``Request:`` line.
    # ``request_text`` is the caller's alias for the same value (the
    # ``user_message`` parameter is authoritative — the version row's
    # message field reads it directly); a blank ``user_message`` degrades
    # to ``request_text`` rather than rendering an empty request line.
    # The app's event loop (this generator runs on it) — ``_on_progress``
    # uses it to schedule the thread-safe ``put_nowait``. Captured before
    # the loop starts: the design loop itself runs on a fresh loop on a
    # worker thread and has no loop to capture from there.
    _loop = asyncio.get_running_loop()
    kwargs: dict[str, Any] = {
        # Issue #355: the production closure's request_logs wrapper reads
        # the design-path row's ``project_id`` from the loop kwargs —
        # the chat seam is the one production caller that carries it.
        "project_id": project_id,
        "photo": photo,
        "chat_history": chat_history,
        "stated_dims": stated_dims,
        "stated_axes": stated_axes,
        "render_fn": None,  # the production closure supplies render_for_design_loop
        "llm_fn": None,
        "bbox_fn": bbox_from_render,
        # Issue #332 (sub-issue 3): the caller's ``request_text`` wins when
        # it DIFFERS from the message (the accepted fill-and-recut offer
        # appends its explicit instruction to the request text while
        # ``user_message`` stays the user's own words — the transcript
        # field). Otherwise the message is authoritative (a blank
        # ``user_message`` degrades to ``request_text``, as before).
        "request": (
            request_text if request_text and request_text != user_message else user_message
        )
        or request_text,
        "on_progress": _on_progress,
        # Issue #417: the loop's per-attempt LLM-call deadline (full
        # design in d33d.design_loop._await_with_per_attempt_deadline);
        # this adapter's deadline below is a generous outer safety net
        # only.
        "attempt_timeout": DESIGN_LOOP_ATTEMPT_TIMEOUT_SECONDS,
    }

    # The production closure (``_build_production_design_loop``) takes
    # ``(app, **kwargs)`` and forwards everything through the hook; the
    # kwargs (including ``on_progress``) are ALWAYS forwarded — a stub
    # that consumes any of them must take ``app`` / ``**kwargs`` like the
    # production closure (the app-parameter check only selects the
    # calling convention, never whether the kwargs arrive — issue #221
    # adversarial round 1: the conditional ``run_loop()`` call silently
    # dropped ``on_progress`` from no-``app`` stubs, breaking the
    # liveness-frame test's acceptance criterion).
    # The carried source (issue #105) is captured here — BEFORE the loop
    # runs — so the loop's prompt renders the current design (turn 1
    # renders the explicit clean-slate wording instead) and the value is
    # the one this turn's prompt actually carried. ``None`` when no
    # version owns a source yet; omitted from the kwargs then (a stub
    # seam without the kwarg is still called cleanly — the production
    # closure forwards **kwargs through the hook to the real loop, and
    # the real loop's ``design_source`` parameter defaults to ``None``
    # anyway, so the kwarg is only added when there IS a source to
    # carry).
    design_source: str | None = None
    row = app.state.conn.get_project(project_id)
    if row is not None:
        from d33d.design_source import current_version_source

        design_source = current_version_source(row, app.state.versions)
    if design_source is not None:
        kwargs["design_source"] = design_source

    # Issue #332 (sub-issue 3) — the import section's inputs: the
    # project's part (assumed/settled → the ``part_scale`` + the v1's
    # measured mm bbox, the ground-truth baseline; no part / unsettled →
    # ``None`` — the byte-identity regression anchor). The single reader
    # is ``d33d.part_http.part_envelope_with_bbox`` (shared with the
    # finalize seam — never two divergent copies). A part whose units are
    # unsettled renders NOTHING (the unsettled pre-route in
    # ``d33d.projects.post_chat`` has already stopped the loop before
    # this seam runs).
    if row is not None:
        from d33d.part_http import part_envelope_with_bbox

        part_env = part_envelope_with_bbox(row, app.state.conn, app.state.versions)
        if part_env is not None:
            kwargs["part_scale"] = part_env["scale"]
            if part_env.get("bbox_mm") is not None:
                kwargs["part_bbox_mm"] = part_env["bbox_mm"]
            elif row.get("part_filename"):
                # Issue #389 (item 4) — the part-less DEGRADE is no longer
                # silent: the project HAS a part (``part_filename`` set,
                # units assumed/settled — ``part_envelope`` filters the
                # unsettled), but the v1 row's bbox could not be resolved
                # (a missing v1 row or a corrupt v1 bbox) and the loop
                # runs WITHOUT the part-baseline bbox. The existing #356
                # photo-missing warning below is the model: one WARNING
                # naming the project id (never a path — never the part's
                # or the project's file paths), so the operator can see
                # the import project's gate ran without the part baseline.
                # A part-LESS project (``part_filename`` NULL) stays
                # silent — that is a normal state, not a degrade.
                logger.warning(
                    "design loop for project %s: the part's v1 bbox could "
                    "not be read (missing v1 row or corrupt v1 bbox) — "
                    "the design loop runs without the part-baseline bbox "
                    "(no part_bbox_mm)",
                    project_id,
                )

    # Issue #386 (final, 2026-10-05) — the through-hole check's BASELINE.
    # Three cases (end to end: design_loop_events → design_loop):
    #
    # 1. NEW DESIGN (no part, no version): kwarg omitted → the loop
    #    defaults to 0.
    # 2. V1 ON AN IMPORT: the GENUS OF THE STORED, REPAIRED PART MESH
    #    (``{repo}/versions/{v1}/part.stl`` — the exact mesh the render
    #    imports). Measured with the same ``mesh_topology`` helper the
    #    v2+ path uses, OFF the event loop. Never ``part_report.
    #    hole_count``: that value is ``gaps_before + genus`` on the
    #    PRE-repair mesh, and the stored mesh's gaps are already closed
    #    by the import-time pymeshfix pass — so ``hole_count`` OVERSTATES
    #    the baseline whenever the import had open gaps (a holey.stl
    #    import's report says 4 while its stored genus is 0; a correct
    #    rendered through-hole of genus 1 does not exceed 4 and would be
    #    wrongly failed as geometrically_wrong). A missing or unreadable
    #    part mesh (or a 3MF import, which stores no STL) passes the
    #    ``-1`` unknown sentinel (the check ABSTAINS); it never falls
    #    back to ``hole_count``.
    # 3. V2+ EDIT (a version has rendered): the PARENT VERSION's rendered
    #    genus. When the version's ``render_artifact_dir`` carries a
    #    ``model.stl`` the seam measures it (OFF the event loop — the
    #    load/split is real disk I/O). When the version was never
    #    rendered (no ``render_artifact_dir``), the baseline ABSTAINS
    #    (``-1`` is passed → the loop abstains) — a fabricated baseline
    #    would make the gate lie.
    #
    # Issue #418: the parent baseline applies to EVERY v2+ edit —
    # designed or imported. The baseline block is no longer gated on
    # ``part_filename``: a scratch (part-less) project whose latest
    # version has a rendered ``model.stl`` gets the parent's rendered
    # genus, not the implicit 0 (a pocket then trivially passed on any
    # designed part that already has a hole). Each run also LOGS the
    # baseline used and its source (the ``through_baseline_genus_source``
    # kwarg — the check logs it with its decision), so QA can see why
    # the check passed or failed.
    if row is not None:
        _latest_ver = (
            app.state.versions.latest_version(project_id)
            if app.state.versions is not None
            else None
        )
        if _latest_ver is not None and _latest_ver.get("render_artifact_dir"):
            # V2+: measure the parent version's rendered genus.
            _render_dir = _latest_ver["render_artifact_dir"]
            _parent_genus = await asyncio.to_thread(
                _measured_genus_for_dir, _render_dir
            )
            if _parent_genus is not None:
                kwargs["through_baseline_genus"] = _parent_genus
                kwargs["through_baseline_genus_source"] = (
                    f"parent version v{_latest_ver['id']} rendered genus: "
                    f"{_parent_genus}"
                )
            else:
                # Abstain: the -1 unknown sentinel (the loop's
                # resolve_baseline_genus maps it to None). A missing
                # parent render is never a fabricated baseline.
                kwargs["through_baseline_genus"] = -1
                kwargs["through_baseline_genus_source"] = (
                    f"parent version v{_latest_ver['id']} rendered mesh "
                    f"unavailable — abstain"
                )
        elif row.get("part_filename"):
            # V1 on import: the stored part mesh's genus (the mesh the
            # render imports). A 3MF import stores no STL (the render
            # re-exports it — no baseline is available) → abstain;
            # ``_stored_part_mesh_path`` filters the non-STL suffix,
            # so no format string compare is needed here.
            _stored_genus: int | None = None
            _stored_path = _stored_part_mesh_path(row, app.state.conn)
            if _stored_path is not None:
                _stored_genus = await asyncio.to_thread(
                    _measured_genus_for_file, str(_stored_path)
                )
            if _stored_genus is not None:
                kwargs["through_baseline_genus"] = _stored_genus
                kwargs["through_baseline_genus_source"] = (
                    f"stored repaired part genus: {_stored_genus}"
                )
            else:
                # Missing / unreadable part mesh (or a 3MF import): the
                # check ABSTAINS. The -1 unknown sentinel is passed (the
                # loop's resolve_baseline_genus maps it to None). NEVER
                # a fallback to the report's ``hole_count`` — a
                # fabricated baseline would make the gate lie.
                kwargs["through_baseline_genus"] = -1
                kwargs["through_baseline_genus_source"] = (
                    "stored part mesh unavailable — abstain"
                )
        else:
            # Issue #418: a part-less project. No version yet → the
            # NEW-design default: the kwarg is omitted (the loop
            # defaults to 0 — "no baseline — new design"). A version
            # exists but was never rendered (no render_artifact_dir,
            # so no measurable parent mesh) → the check abstains — a
            # fabricated baseline would make the gate lie.
            if _latest_ver is not None:
                kwargs["through_baseline_genus"] = -1
                kwargs["through_baseline_genus_source"] = (
                    f"parent version v{_latest_ver['id']} never rendered "
                    f"— abstain"
                )

    # Issue #295 — the lost-photo notice (the post_chat caller's photo
    # state, carried into the stream): when the project's stored photo
    # was LOST out-of-band (path set, file gone — never a photo-LESS
    # project), the terminal stream carries ONE plain copy.ts notice
    # BEFORE the done/error frame. The design proceeds photo-less with
    # the EMPTY_PHOTO_DATA_URI constant, unchanged behaviour — only the
    # notice and the WARNING (project id + photo FILE NAME — never the
    # full path, never the bytes; issue #356) are new.
    # The notice is deferred until just before the terminal frame on
    # every exit path (pass, exhausted, exception, deadline): the run's
    # own frames (progress, version-created, tokens) always land first,
    # so the notice never precedes the run's progress — and the terminal
    # frame always carries the notice's last word right before it.
    _photo_notice: str | None = None
    if row is not None and photo_lost(row):
        # Issue #356 — the WARNING names the project id AND the photo
        # FILE NAME (the path's basename — never the full path, never
        # the bytes): the operator matching the live log can now spot
        # the undecodable file by name.
        logger.warning(
            "design loop for project %s: the stored reference photo %s "
            "is missing on disk or undecodable — the design proceeds "
            "photo-less; the stream carries the copy.ts notice",
            project_id,
            Path(row.get("source_photo_path")).name,
        )
        from d33d.design_frames import PHOTO_MISSING_NOTICE

        _photo_notice = PHOTO_MISSING_NOTICE

    def _yield_notice() -> list[tuple[str, dict[str, Any]]]:
        nonlocal _photo_notice
        if _photo_notice is None:
            return []
        frames = [("notice", {"message": _photo_notice})]
        _photo_notice = None
        return frames

    # The design-state block's inputs (issue #120/#137/#246/#248 — the
    # SAME values ``_finalize_loop_kwargs`` passes on the finalize path):
    # the latest version's full params snapshot, persisted measured bbox,
    # persisted per-axis stated set, and persisted per-parameter
    # metadata, read ONCE here so the live chat prompt, the finalize
    # prompt and the GET the SPA reads all render the SAME block via
    # ``state_block_for_version``. All ``None`` when no version exists
    # yet (the block then renders its honest empty state — never a
    # fabricated dimension).
    latest = app.state.versions.latest_version(project_id)
    kwargs["state_params"] = dict(latest["params"]) if latest is not None else None
    kwargs["state_bbox"] = latest["bbox"] if latest is not None else None
    kwargs["state_stated"] = latest["stated_dims"] if latest is not None else None
    kwargs["state_meta"] = latest["param_meta"] if latest is not None else None
    # The version's param-keyed confirmed set (issue #250, rule (b)): the
    # same value the design-state route reads — the live prompt's block
    # renders a confirmed param ``stated`` exactly as the SPA's Brief does.
    # Read BEFORE the pass, so it is also the PREVIOUS version's set the
    # pass branch's ``_resolve_offer`` uses for selection: a confirmation
    # the user made on the previous version is confirmed evidence, excluded
    # from the new version's offer (the new row's own ``confirmed_params``
    # stays NULL until the user accepts THAT version's offer).
    kwargs["state_confirmed"] = (
        latest["confirmed_params"] if latest is not None else None
    )
    # The pre-pass confirmed set is kept in the local ``kwargs`` (issue
    # #250's cross-project race): it feeds ONLY this pass's offer
    # selection (``_resolve_offer(prev_confirmed=...)`` below). It is
    # deliberately NOT stashed in ``app.state`` — with two projects'
    # passes interleaved on the shared app, an ``app.state`` stash is
    # project-global and a pass on project B would read project A's
    # pre-pass set.
    try:
        if _loop_takes_app(run_loop):
            raw = run_loop(app=app, **kwargs)
        else:
            raw = run_loop(**kwargs)
        if inspect.isawaitable(raw):
            # The real loop is async: ``raw`` is a coroutine. The spec's
            # acceptance criterion — "the background task wraps the sync
            # render_for_design_loop in asyncio.to_thread (it does Docker
            # subprocess work, multi-minute)" — is implemented here. A bare
            # ``await raw`` would run the sync ``render_for_design_loop``
            # (Docker ``subprocess.run``, minutes) on the event loop and
            # stall every other SSE stream and API handler on the app, not
            # just the in-flight one.
            #
            # ``asyncio.to_thread`` cannot ``await`` a coroutine directly (a
            # worker thread has no running event loop to nest an
            # ``asyncio.run`` under — it would raise ``RuntimeError``), so
            # the work is run via ``to_thread(_run_in_loop, raw)``:
            # ``_run_in_loop`` drives the coroutine with ``asyncio.run`` on
            # a fresh loop, in a worker thread, so the entire design loop —
            # the multi-minute sync render AND the LLM's awaits — runs off
            # the app's event loop. The event loop's role is to pump the
            # frame queue while the render task runs: the ``to_thread``
            # work is wrapped in a ``Task`` and the generator
            # ``asyncio.wait``s it against a ``_frame_queue`` ``get``, so
            # the per-view frames are yielded WHILE the render is still
            # running (live progress — the old ``await
            # asyncio.to_thread(...)`` hoarded them and delivered the whole
            # batch in a single instant at render completion).
            render_task = asyncio.ensure_future(asyncio.to_thread(_run_in_loop, raw))
            _attempt_tracker = AttemptTracker(_loop, MAX_ITERATIONS)
            # The total wall-clock deadline (issue #221) — measured from
            # HERE (generator start, before the first drain), never
            # reset by incoming frames. The deadline must race against
            # BOTH the ``to_thread`` render task AND the frame-queue
            # consumer: a loop that keeps emitting liveness frames
            # (per-view progress, LLM tokens) while it never terminates
            # would evade a timeout placed only around ``render_task``.
            # Issue #396 — the deadline emission must ALSO archive the
            # stalled run to the failures.jsonl sink. The hook the app
            # wires at the loop seam (``record_production_failure``)
            # only sees a loop RESULT, and a deadline kill produces
            # none (the loop never returned), so without this the
            # whole-loop timeout would never reach the archive while
            # every other failure class does. Best-effort: a failure
            # here is logged and swallowed — the deadline frame is the
            # user-visible guarantee (the hook's errors-swallowed
            # contract elsewhere).

            # The loop's OWN per-attempt deadline (issue #417 — full
            # design in
            # d33d.design_loop._await_with_per_attempt_deadline) does the
            # actual cutting off for a slow model: ``run_design_loop_async``
            # returns a best-so-far exhausted result with the structured
            # ``design_loop_timed_out`` reason, and the ordinary
            # exhaustion path below surfaces it (and version-creates the
            # kept candidate when one rendered). This adapter deadline is
            # a GENEROUS OUTER SAFETY NET — the derived total (per-attempt
            # budget × iteration cap) plus a fixed margin, and below the
            # client's ``STREAM_TOTAL_TIMEOUT_MS`` (web/src/lib/api.ts)
            # with documented headroom — that fires only for a run that
            # stops yielding frames before the loop's own deadline can
            # (a thread stuck in a blocking call, not a slow model). A
            # legitimately slow run that finishes within its budget is
            # never cut off here.
            _total_deadline = (
                DESIGN_LOOP_ATTEMPT_TIMEOUT_SECONDS * MAX_ITERATIONS
                + ADAPTER_DEADLINE_MARGIN_SECONDS
            )
            _deadline = _loop.time() + _total_deadline

            async def _deadline_outcome() -> list[tuple[str, dict[str, Any]]]:
                """The adapter-deadline cut-off (issue #417): the OUTER
                SAFETY NET — it fires only for a run that stopped
                yielding frames before the loop's own per-attempt
                deadline could (a thread stuck in a blocking call; a
                slow model is handled by the loop, which returns its
                best-so-far result through the ordinary exhaustion
                path below). Archive the stall, then the terminal frames
                in FINAL ORDER: [photo notice(s), terminal error].

                The safety net does NOT create a version: it only
                archives the stall and emits the terminal error frame.
                The loop's own timeout-version path (the primary
                slow-model path) is the sole version-creation point for
                a timed-out run.

                Returns the terminal frames (the caller yields them and
                then cancels the ``to_thread`` task — the worker thread
                cannot be killed mid-flight, so the cancel runs only
                after the user-visible guarantee has been emitted).
                """
                _latencies = _attempt_tracker.latencies()
                _attempt_count = _attempt_tracker.attempt_count
                logger.warning(
                    "design loop for project %s exceeded the %ss "
                    "adapter safety-net deadline (per-attempt %ss × %s "
                    "attempts + %ss margin; measured %r s per attempt) — "
                    "emitting terminal design_loop_timed_out frame",
                    project_id,
                    _total_deadline,
                    DESIGN_LOOP_ATTEMPT_TIMEOUT_SECONDS,
                    MAX_ITERATIONS,
                    ADAPTER_DEADLINE_MARGIN_SECONDS,
                    _latencies,
                )
                archive_deadline(
                    app,
                    project_id,
                    kwargs=kwargs,
                    photo=photo,
                    attempt_tracker=_attempt_tracker,
                    deadline_reason=DESIGN_LOOP_TIMED_OUT_REASON,
                    attempt_count=_attempt_count,
                    latencies=_latencies,
                )
                _avg_latency = (
                    sum(_latencies) / len(_latencies) if _latencies else None
                )
                _error_data: dict[str, Any] = {
                    "message": (
                        "Design loop timed out after "
                        f"{int(_total_deadline)}s"
                    ),
                    "reason": DESIGN_LOOP_TIMED_OUT_REASON,
                }
                if _avg_latency is not None:
                    _error_data["attempt_latency_seconds"] = round(_avg_latency)
                if _attempt_count > 0:
                    _error_data["attempt_count"] = _attempt_count
                frames: list[tuple[str, dict[str, Any]]] = _yield_notice()
                frames.append(("error", _error_data))
                return frames

            while True:
                if render_task.done():
                    break
                try:
                    _f = _frame_queue.get_nowait()
                except asyncio.QueueEmpty:
                    _f = None
                if _f is not None:
                    _attempt_tracker.observe(_f)
                    yield _f
                    continue
                _remaining = _deadline - _loop.time()
                if _remaining <= 0:
                    # Deadline fired: cut off the stream with a terminal
                    # structured error frame. No further frames may be
                    # yielded after this one (the deadline frame is
                    # terminal by contract).
                    _deadline_frames = await _deadline_outcome()
                    for _f in _deadline_frames:
                        yield _f
                    # Cancel the ``to_thread`` render task AFTER the frame
                    # has been yielded. CANCELLING IT BEFORE the yield
                    # deadlocks the executor thread-pool: the ``to_thread
                    # `` future's cancellation hooks run on the loop and
                    # wait for the executor's future to report the
                    # cancellation, but the executor thread is still
                    # running the (stalled) loop — a ``CancelledError``
                    # raised in that future blocks the pool's thread
                    # (measured: the test executor hangs for the full
                    # 300s shutdown join). Yielding first means the
                    # generator (and any caller's ``break``) has already
                    # completed the user-visible guarantee; the cancel
                    # then runs on a free loop. The ``to_thread`` worker
                    # thread cannot be killed mid-flight regardless (the
                    # terminal frame is the user-facing guarantee, not
                    # the thread's death), and the best-effort cancel
                    # suppresses the ``CancelledError`` so it does not
                    # surface as an unhandled exception when the loop
                    # closes.
                    _suppress_cancellation(render_task)
                    return
                # Empty queue, render still running: sleep until the next
                # frame is enqueued, the render task completes, or the
                # deadline fires — the ``asyncio.wait`` is a real await
                # (no busy-wait) that wakes on whichever happens first.
                _get_task = asyncio.ensure_future(_frame_queue.get())
                _done, _ = await asyncio.wait(
                    {_get_task, render_task},
                    return_when=asyncio.FIRST_COMPLETED,
                    timeout=_remaining,
                )
                if _get_task in _done:
                    _f = _get_task.result()
                    if _f is None:
                        # Sentinel: the render finished (the drain thread
                        # enqueued the ``None`` before the loop exited).
                        break
                    _attempt_tracker.observe(_f)
                    yield _f
                    continue
                _get_task.cancel()
                # Render task completed, or the deadline fired (timeout).
                # The deadline check at the top of the loop handles the
                # timeout; otherwise the loop re-checks ``render_task``
                # and the remainder is drained below.
            # The render finished: yield every remaining frame in FIFO
            # order (all frames were enqueued before the drain thread
            # joined, so nothing is lost) and take the render's result
            # (an exception here propagates to the wide catches below,
            # unchanged).
            while True:
                try:
                    _f = _frame_queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                if _f is None:
                    # Sentinel: stop the drain, take the result.
                    break
                _attempt_tracker.observe(_f)
                yield _f
            result = render_task.result()
        else:
            result = raw
    except (LookupError, KeyError) as e:
        # Project (or a loop prerequisite) deleted mid-flight — the
        # finalize pattern's ``assert row is not None`` raises
        # AssertionError/LookupError after deletion. Emit a terminal
        # error frame + release the flag, not a 500.
        logger.exception("design loop infra error for project %s", project_id)
        for _f in _yield_notice():
            yield _f
        yield ("error", {"message": f"design loop infra failure: {e}"})
        return
    except (OSError, RuntimeError, TypeError, ValueError) as e:
        logger.exception("design loop failed for project %s", project_id)
        for _f in _yield_notice():
            yield _f
        yield ("error", {"message": f"design loop failed: {e}"})
        return
    except Exception as e:  # broad by contract — every exit is a terminal frame
        logger.exception("design loop unexpected error for project %s", project_id)
        for _f in _yield_notice():
            yield _f
        yield ("error", {"message": f"design loop unexpected error: {e}"})
        return
    if getattr(result, "status", None) == "pass":
        yield ("progress", {"step": "design-loop-pass"})
        best = getattr(result, "best", None)
        # The best candidate's OWN render artifacts (the best iteration's
        # STL + 6 views — never a later iteration's) are read from the
        # durable artifact directory the worker persisted to inside its
        # ``tempfile.TemporaryDirectory`` with-block (issue #72 — the
        # bytes survive past the tempdir teardown, so the adapter reads
        # them from the persistent path, not the dead tempdir paths).
        # A missing/dead render → fields omitted entirely (never a
        # bogus path, never a null), keeping the frame JSON-safe.
        render = getattr(best, "render", None)
        frame_fields: dict[str, Any] = {}
        if render is not None:
            stl_bytes, view_bytes = _artifact_bytes_from_path(render)
            if stl_bytes is not None:
                frame_fields["stl_data_uri"] = _data_uri_from_bytes(
                    stl_bytes, "application/octet-stream"
                )
            if view_bytes:
                frame_fields["views"] = {
                    name: _data_uri_from_bytes(raw, "image/png")
                    for name, raw in view_bytes.items()
                }
        # Ticket #91 / issue #247: propagate the bbox abstention to the
        # wire so a consumer can never mistake an abstained pass for a
        # verified one — ``Score.bbox_abstained`` exists on the score, but
        # a flag that stops at the Score object is not a safeguard: every
        # pass frame carries the flag in both the version-created progress
        # frame and the terminal done frame. Per-axis semantics (issue
        # #247): the flag is False ONLY for a fully confirmed, fully
        # measured pass; True when no axis was checked at all (the gate
        # abstained entirely) AND when SOME axes were checked but others
        # were not confirmed (a partial pass still carries unmeasured
        # axes — the per-axis reality). A per-axis breakdown is NOT added
        # to the frame: the frame schema is frozen (SEAM D tests), and the
        # flag plus the version row's persisted ``stated_dims`` column
        # (issue #246) together carry the per-axis truth. (The
        # failures.jsonl archive needs no field: it fires ONLY on
        # ``exhausted`` results, and an abstained bbox bit is always
        # True, so an abstained gate never lands there as a failure —
        # it can only make a loop more pass-prone.)
        score = getattr(best, "score", None)
        abstained = bool(getattr(score, "bbox_abstained", False))
        # The previous version BEFORE this pass creates one (the offer
        # selection's diff baseline and the confirmed-set carry-forward
        # source — issue #250), read once.
        prev_version = app.state.versions.latest_version(project_id)
        version_id = await _resolve_version_create(
            app,
            project_id,
            result,
            user_message,
            stated_axes=stated_axes,
        )
        # The assumed-value offer (issue #250): pick AT MOST ONE assumed
        # param of the NEW version, build its sentence (the model's
        # ``confirm_sentence`` when the number guard passes, else the
        # deterministic template), and persist the pending offer
        # server-side. Never on a stale offer (a newer version supersedes
        # it — this IS a new version, so a prior pending offer lapses and
        # is replaced by this one, or cleared when no param qualifies).
        # Best-effort: any offer-path failure logs and degrades to no
        # offer (the pass itself is unaffected).
        offer_field: dict[str, Any] | None = None
        try:
            offer_field = await _resolve_offer(
                app,
                project_id,
                version_id,
                result,
                prev_version,
                prev_confirmed=kwargs["state_confirmed"],
                user_message=user_message,
                chat_history=chat_history,
            )
        except Exception:
            logger.exception(
                "offer resolution failed for project %s — emitting the "
                "pass without an offer",
                project_id,
            )
            offer_field = None
        if version_id is not None:
            vc_frame: dict[str, Any] = {
                "step": "version-created",
                "version_id": version_id,
            }
            vc_frame.update(frame_fields)
            vc_frame["bbox_abstained"] = abstained
            yield ("progress", vc_frame)
        scad = getattr(best, "scad_source", None)
        yield ("token", {"text": scad if isinstance(scad, str) else ""})
        done_data: dict[str, Any] = {
            "message": _result_message(result),
            "bbox_abstained": abstained,
        }
        # The offer rides the done frame as ADDITIVE fields (the SPA's
        # ``App.tsx`` routes ``confirm_offer`` + ``confirm_sentence`` to a
        # separate plain assistant message after the pass card — issue
        # #250's task-b contract; existing frames carry no ``confirm_*``
        # keys at all, byte-identical when no offer was made).
        if offer_field is not None:
            done_data["confirm_offer"] = offer_field["param"]
            done_data["confirm_sentence"] = offer_field["sentence"]
        for _f in _yield_notice():
            yield _f
        yield ("done", done_data)
    else:
        # Exhausted (or otherwise non-pass): a failed run can carry no
        # offer — a stale pending offer lapses (the acceptance check
        # requires a live offer on the CURRENT latest version, and a
        # subsequent passing pass will set a fresh one).
        try:
            app.state.versions.set_pending_offer(project_id, None)
        except Exception:  # noqa: BLE001 — clearing is best-effort
            logger.debug("failed to clear pending offer for project %s", project_id)
        # Exhausted (or otherwise non-pass): the terminal error frame gains
        # the STRUCTURED failure reason (issue #82) so the SPA can map it
        # to plain-language copy without string-matching the free-text
        # ``message`` (which is preserved verbatim for backward
        # compatibility). A ``None`` reason → the field is OMITTED (never a
        # null), matching the adapter's omit-not-null frame policy.
        error_data: dict[str, Any] = {"message": _result_message(result)}
        reason = _structured_reason(result)
        if reason is not None:
            error_data["reason"] = reason
        # The missing/empty ``${ENV}`` variable name (issue #303): the
        # model pre-flight's ``ModelPreflight.env_var`` rides the result as
        # ``env_var`` so the SPA's ``FailureTurn`` can render the helper
        # sentence ("Set <NAME> where the server runs, then restart it.")
        # via ``copy.failure.reasons.model_unconfigured``. Omitted for every
        # other reason (omit-not-null); omitted when the pre-flight could
        # not name a variable (unresolved role/alias → the "Check the model
        # settings." copy).
        env_var = getattr(result, "env_var", None)
        if reason == MODEL_UNCONFIGURED and isinstance(env_var, str) and env_var:
            error_data["env_var"] = env_var
        # The verified render-worker image fault (issue #346): the
        # pre-flight's structured ``renderer_detail`` (``image_missing`` vs
        # ``label_mismatch`` + the exact rebuild command) rides the result
        # as ``renderer_detail`` so the SPA's ``FailureTurn`` can render
        # the real reason + rebuild command in the mono face. Omitted for
        # every other reason and when the pre-flight produced no fault
        # detail (omit-not-null).
        renderer_detail = getattr(result, "renderer_detail", None)
        if (
            reason == RENDERER_IMAGE_STALE
            and isinstance(renderer_detail, dict)
            and renderer_detail
        ):
            error_data["renderer_detail"] = {
                key: value
                for key, value in renderer_detail.items()
                if isinstance(value, str) and value
            }
        # The gate's per-axis enforced set (issue #261 fix batch): lets
        # the failure copy name the value that was HELD when a carried
        # axis fails ("I kept the height you set earlier (12.0 mm)").
        # Omitted for every non-bbox failure and when the gate set is
        # empty (omit-not-null). The values are the user's own stated
        # numbers — safe to render with the SPA's ``mm()`` formatter.
        carried = _carried_axes(result, kwargs.get("stated_axes"))
        if carried is not None:
            error_data["carried_axes"] = carried
        # The gate's ACTUALLY-COMPARED extents (issue #367): the made
        # values the SPA renders beside the asked ones in the
        # size-mismatch card (best-matching component for a full
        # confirmed triple, whole-mesh extents for a partial set — the
        # gate's own selection). Omitted for every other reason and
        # when nothing was measured (omit-not-null).
        measured = _measured_axes(
            result,
            kwargs.get("stated_axes"),
            kwargs.get("part_bbox_mm"),
        )
        if measured is not None:
            error_data["measured_axes"] = measured
        # The per-param mismatch detail (issue #276): one line per
        # mismatching param (label + both numbers, the server's own
        # gate evidence), so the SPA's failure turn renders the detail
        # without re-deriving any number. Omitted for every other
        # reason (omit-not-null).
        # The per-param mismatch detail (issue #276): structured
        # ``mismatches`` entries (label + model + measured + axis, the
        # server's own gate evidence) so the SPA renders one line per
        # mismatch via its own copy helper without re-deriving any number.
        # Omitted for every other reason (omit-not-null).
        mismatches = _axis_mismatches(result)
        if mismatches is not None:
            error_data["mismatches"] = mismatches
        # Issue #417 — the timeout-version path: when the loop's own
        # per-attempt LLM-call deadline fired (full design in
        # d33d.design_loop._await_with_per_attempt_deadline — a slow
        # model hits it, the adapter's deadline below never does) and
        # the loop kept a rendered best candidate
        # (a real, scored ``IterationRecord`` — never unvalidated text),
        # version it BEFORE the terminal error frame — the same
        # frame order the adapter-deadline path uses (version-created
        # before the error: the ticket's gate resolution). An
        # exhausted loop's ``best`` is NOT versioned today (pass is the
        # sole version trigger); this is the NEW timeout-version path
        # that names it. Only a candidate that actually RENDRED is
        # version-grade (``render`` not ``None`` — the synthetic
        # pre-flight placeholder and fail-fast empty-scad records carry
        # ``render=None``/empty SCAD and are never kept); with zero
        # rendered candidates the run ends without a version. The
        # structured ``design_loop_timed_out`` reason stays on the
        # terminal error frame — the SPA renders the version and the
        # slow-model copy (with the measured per-attempt latency the
        # frame carries) together, failure first.
        #
        # The numbers are LOOP-SOURCED (issue #417): the loop owns the
        # wall clock — ``result.attempts_started`` (the attempts it
        # STARTED, including the one killed mid-LLM-call) and
        # ``result.attempt_latencies`` (one measured value per started
        # attempt; a killed attempt keeps the budget it burned, a
        # completed one its full duration). The frame's
        # ``attempt_latency_seconds`` is the MEAN of those values (the
        # copy says "about Ns an attempt" — a mean, not a max, not a
        # last). The frame-derived ``_attempt_tracker`` is only a
        # FALLBACK for results that carry no loop-sourced numbers
        # (non-deadline-shaped results, or a stub loop the loop's own
        # deadline did not cut — the adapter safety-net path is the
        # only other timeout path, and it builds its own frame).
        _timeout_kept_version_id: int | None = None
        if (
            reason == DESIGN_LOOP_TIMED_OUT_REASON
            and getattr(result, "status", None) == "exhausted"
        ):
            _kept_best = getattr(result, "best", None)
            _kept_render = getattr(_kept_best, "render", None)
            _kept_scad = getattr(_kept_best, "scad_source", None)
            if (
                _kept_best is not None
                and _kept_render is not None
                and isinstance(_kept_scad, str)
                and _kept_scad.strip()
                and scad_looks_valid(_kept_scad)
            ):
                try:
                    _timeout_kept_version_id = await _resolve_version_create(
                        app,
                        project_id,
                        result,
                        user_message,
                        stated_axes=stated_axes,
                    )
                except Exception:
                    logger.exception(
                        "timeout-kept version creation failed for "
                        "project %s — emitting the timeout frame "
                        "without a version",
                        project_id,
                    )
                    _timeout_kept_version_id = None
                # The slow-model copy's measured per-attempt latency
                # ("about Ns an attempt" — the SPA renders it from the
                # terminal frame's ``attempt_latency_seconds`` /
                # ``attempt_count`` fields, both omit-not-null): the
                # LOOP-SOURCED numbers above — the mean of the loop's
                # own per-attempt wall clock, rounded up to a whole
                # second (a killed attempt's 0.2 s burns read as
                # "about 1s an attempt", never "about 0s"). Omitted
                # when no attempt was measured (an unmeasured number
                # would violate the SPA's "never render a number the
                # SPA has not established" invariant).
                _loop_latencies = getattr(result, "attempt_latencies", None)
                if _loop_latencies:
                    _avg_latency = sum(_loop_latencies) / len(_loop_latencies)
                    # Round UP to a whole second: a killed attempt that
                    # burned 0.2 s of a 0.2 s budget reads as "about 1s
                    # an attempt", never "about 0s" — and a real slow
                    # model's 58.2 s average never reads as "58s" when
                    # it was really 59s.
                    if _avg_latency > 0:
                        error_data["attempt_latency_seconds"] = math.ceil(
                            _avg_latency
                        )
                    _started = getattr(result, "attempts_started", None)
                    if not isinstance(_started, int) or _started <= 0:
                        _started = len(_loop_latencies)
                    error_data["attempt_count"] = _started
                else:
                    # Fallback: the frame-derived attempt tracker (the
                    # adapter safety-net path — see the comment above).
                    _latencies = _attempt_tracker.latencies()
                    _avg_latency = (
                        sum(_latencies) / len(_latencies) if _latencies else None
                    )
                    if _avg_latency is not None:
                        error_data["attempt_latency_seconds"] = round(_avg_latency)
                    if _attempt_tracker.attempt_count > 0:
                        error_data["attempt_count"] = _attempt_tracker.attempt_count
        # Frame order (issue #417 gate resolution): [photo notice(s),
        # version-created (when the kept candidate was stored), terminal
        # error] — the SAME order the adapter-deadline path uses, and the
        # SPA handles it: the version-created frame refetches the
        # timeline (the kept version appears in the Brief), then the
        # terminal error renders the failure turn with the slow-model
        # copy beside the version the frame just announced.
        if _timeout_kept_version_id is not None:
            yield (
                "progress",
                {
                    "step": "version-created",
                    "version_id": _timeout_kept_version_id,
                },
            )
        for _f in _yield_notice():
            yield _f
        yield ("error", error_data)


__all__ = [
    "EMPTY_PHOTO_DATA_URI",
    "axes_to_gate_triple",
    "bbox_from_render",
    "latest_version_stated_dims",
    "photo_data_uri",
    "photo_lost",
    "photo_storage_signal",
    "run_design_loop_with_events",
    "validate_photo_bytes",
]
