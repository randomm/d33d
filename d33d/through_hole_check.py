"""Deterministic through-hole genus post-check (issue #386).

The post-check runs on the ok-render branch of the design loop, BEFORE
the ``score.perfect`` pass return — a blind pocket in place of a
requested through-hole is invisible to all five gate bits (the QA
2026-10-05 repro: a 5 mm pocket over an 80 mm block passed as "built
and checked"), so without this check a pocket passes silently.

Operator decision 2026-10-05 (binding): the rendered mesh's total genus
must EXCEED the parent version's baseline genus — ``0`` for a new
design, the GENUS OF THE STORED, REPAIRED PART MESH for v1 on an
import (never ``part_report.hole_count``, which overstates the
baseline whenever the import had open gaps). This REPLACES "genus ≥
1", which would let a pocket pass on any part that already has a hole
(the plate has genus 3, the knob 1).

The module is deliberately separate from ``d33d.design_loop`` (the loop
orchestrates; this module owns the check's logic), mirroring
``d33d.screw_hole_check`` (the #317 pattern: a detection-only module
returning a tuple or ``None``, the caller folding it into the repair
dict). The shared mesh-topology helper from issue #395
(``d33d.part_mesh_topology.mesh_topology``) supplies the genus
measurement — one helper for both the import-path clean-skip and this
check.

The trigger runs against the ``request`` kwarg EXACTLY as the design
loop receives it (the operator decision 2026-10-05: the user message
plus any appended system instruction — the fill-and-recut instruction
contains no "through", so no stripping is needed).
"""

from __future__ import annotations

import logging
import re

from d33d.part_holes import HOLE_NOUNS

logger = logging.getLogger(__name__)

__all__ = [
    "THROUGH_HOLE_INSTRUCTION",
    "is_through_request",
    "requested_hole_count",
    "resolve_baseline_genus",
    "route_through_hole_repair",
    "through_hole_check",
]

#: The hyphenated through-hole tokens that fire on their own (no hole
#: noun required) — whole-word, case-insensitive.
_HYPHENATED_TOKENS = ("through-hole", "thru-hole")

#: The bare hole nouns as whole-word, case-insensitive tokens (the
#: ``is_through_request`` trigger set, unchanged from ``HOLE_NOUNS``).
_BARE_HOLE_NOUNS = tuple(HOLE_NOUNS)

#: The hole vocabulary shared by all three classification functions:
#: every token that can fire ``is_through_request`` (the bare hole nouns
#: from ``part_holes.HOLE_NOUNS`` plus the hyphenated through-hole tokens)
#: is ALSO inspected by ``_request_is_existing_hole`` and
#: ``requested_hole_count``. Routing all three through this one tuple
#: prevents a token that one function classifies from being invisible to
#: the others (issue #418, lens finding 2).
_HOLE_VOCABULARY: tuple[str, ...] = (*HOLE_NOUNS, *_HYPHENATED_TOKENS)

# The -1 abstain sentinel for a baseline genus: -1 means the baseline is
# unknown/unmeasurable (abstain); >= 0 is a measured genus. The alias
# documents the sentinel so callers and tests can rely on the same
# meaning of a negative value.
BaselineGenus = int

#: The ``_HOLE_VOCABULARY`` tokens as a single regex alternation — each
#: token normalised to a regex-safe literal (the singular form with a
#: trailing ``s`` stripped, so ``s?`` covers both forms; the hyphen
#: escaped for regex use). Used by ``requested_hole_count`` for the
#: adjacent-number parse.
_HOLE_VOCABULARY_REGEX = r"(?:" + "|".join(
    re.escape(t.rstrip("s")) + "s?" for t in _HOLE_VOCABULARY
) + r")"


def is_through_request(request: str) -> bool:
    """Does the CURRENT user message ask for a through-hole?

    Fires (case-insensitive, whole-word) when the message contains
    ``through`` together with a bare hole noun from ``HOLE_NOUNS``
    (hole, holes, bore, counterbore), or the hyphenated token
    ``through-hole`` / ``thru-hole``.

    Never fires on ``through`` alone (``throughput``, ``thorough``,
    ``throughout`` — whole-word matching), nor on a hole noun without
    ``through`` (``blind hole``, ``pocket``, ``counterbore 5 mm deep``).
    SCAD comments and parameter names are never a trigger surface — the
    caller passes the loop's ``request`` argument and nothing else.
    """
    if not request:
        return False
    lower = request.lower()
    for token in _HYPHENATED_TOKENS:
        if token in lower:
            # Whole-word: the character immediately BEFORE the token (if
            # any) must not be ALPHANUMERIC — ``x-through-hole`` is a
            # different (non-through-hole) word, ``3-through-hole`` a
            # dimension, and a plain hyphenated compound (``a
            # through-hole``) fires like the bare token. The token's own
            # internal hyphen is not a boundary: it is part of the
            # token, not a separator between words.
            idx = lower.find(token)
            if idx == 0 or not lower[idx - 1].isalnum():
                return True
    words = set(lower.split())
    if "through" not in words:
        return False
    return any(noun in words for noun in _BARE_HOLE_NOUNS)


def _request_is_existing_hole(request: str) -> bool:
    """Issue #418: does the request target an EXISTING hole rather than
    asking for a new one?

    True (the existing-hole branch) ONLY when the hole phrase is preceded
    by the definite article (``the``) AND the token is a hyphenated
    compound (``through-hole`` / ``thru-hole``) — "make the through-hole
    8 mm", "move the through-hole to the left". The hyphenated compound
    names a specific, already-existing hole; the user is modifying it.

    False (the new-hole branch) for:
    * an indefinite article ("a hole", "an 8 mm hole"), a stated count
      ("two holes"), or no article at all ("drill a 6 mm hole through
      the middle", "resize a hole through the plate");
    * ``the`` + a BARE hole noun ("drill the hole through", "make the
      hole go through", "the hole through, please"). A bare noun with
      ``the`` and a separate word ``through`` in the request describes
      the act of cutting through — a NEW-hole request (the gate's stated
      purpose). The definite article marks specificity ("the hole I
      mentioned"), not existence: the user is asking for a hole to be
      drilled, not referring to one already present.

    The classification is article-conditional for hyphenated tokens and
    always new-hole for bare nouns, NOT verb-conditional: a marker verb
    in a different clause ("move the plate", "resize the lid") has no
    hole noun at all and is not a trigger. Runs only on text that already
    fired :func:`is_through_request`.
    """
    lower = request.lower()
    for noun in _HYPHENATED_TOKENS:
        idx = lower.find(noun)
        while idx != -1:
            j = idx - 1
            while j >= 0 and lower[j].isspace():
                j -= 1
            if j >= 2:
                span = lower[j - 2 : j + 1]
                if span == "the":
                    before = lower[j - 3] if j >= 3 else " "
                    if not before.isalnum():
                        return True
            idx = lower.find(noun, idx + 1)
    return False


def requested_hole_count(request: str) -> int:
    """Issue #418: the number of holes the request asks for (``1`` when
    no count is stated).

    A stated count — the first numeral or number word before the first
    hole noun that is NOT a dimension (a number + unit), e.g. "two
    holes", "3 holes", "four through-holes" — is the count. Not "the
    number immediately before the noun": the parser scans the whole
    prefix before the first hole noun and skips any number that is
    followed by a unit (mm, cm, in, ") — a dimension, not a count —
    so "drill 2mm holes" and "drill a 2.5 mm hole" both leave the count
    unstated → ``1``, and a number in a LATER clause ("drill a 2.5mm
    hole and three holes") is not the requested count — the parser
    reads only the prefix before the first hole noun. Everything else
    ("a hole", "the through-hole", no article) is ``1``.
    """
    number_words = {
        "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
        "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    }
    lower = request.lower()
    noun = _HOLE_VOCABULARY_REGEX
    first_hole = re.search(r"(?<![\w-])" + noun + r"s?\b", lower)
    if first_hole is None:
        return 1
    prefix = lower[: first_hole.end()]
    # Units that make a number a dimension, not a count.
    units = r"(?:mm|cm|in|inch|\")"

    # 1. A number word before the first hole noun that is NOT immediately
    #    followed by a unit (a number + unit is a size, not a count):
    #    "two holes", "three 5 mm holes", "four through-holes".
    for word, n in number_words.items():
        if re.search(r"\b" + word + r"\b(?!" + units + r")", prefix):
            return n

    # 2. An Arabic numeral before the first hole noun that is NOT followed
    #    by a unit (a number + unit is a size, not a count) and is not a
    #    decimal fraction (a dimension): "2 holes", "3 through-holes".
    #    The lookbehind rejects a digit that is part of a longer number.
    m = re.search(
        r"(?<![\d.\w-])(\d+)(?!\s*(?:" + units + r")|\d|\.\d)",
        prefix,
    )
    if m:
        n = int(m.group(1))
        if n >= 1:
            return n

    # 3. No number before the first hole noun — any number that IS
    #    followed by a unit is a dimension, not the count.
    return 1


def _rendered_genus(stl: str | None) -> int | None:
    """The rendered mesh's total genus, via the shared
    :func:`d33d.part_mesh_topology.genus_from_stl` helper.

    ``None`` on a missing/unreadable STL (or no STL at all — ``stl`` is
    ``None``), a split failure, or a split with zero watertight
    components (the abstain cases — the helper logs a line and the
    candidate proceeds).

    ANY unexpected exception (``TypeError`` / ``IndexError`` /
    ``MemoryError`` from a malformed mesh) makes the check ABSTAIN —
    the design loop must never crash on a mesh the render worker
    already accepted.
    """
    if stl is None:
        return None
    from d33d.part_mesh_topology import genus_from_stl

    return genus_from_stl(stl)


#: The through-hole post-check's (issue #386, operator decision
#: 2026-10-05) repair instruction: the hole the user asked for does not
#: pass — the rendered mesh's genus did not rise above the baseline — so
#: the next iteration must cut it through the full thickness.
THROUGH_HOLE_INSTRUCTION = (
    "the hole does not pass all the way through the part — it must cut "
    "through the full thickness (a blind pocket or partial depth does "
    "not satisfy a through request; verify the cutting solid extends "
    "beyond both faces of the part)."
)


def resolve_baseline_genus(through_baseline_genus: BaselineGenus | None) -> BaselineGenus | None:
    """The baseline genus for the through-hole check (issue #386, operator
    decision 2026-10-05; issue #418).

    * ``0`` — no explicit seam value: a NEW design (a baseline-less
      candidate; the ``design_loop_events`` chat seam omits the kwarg
      only for a project that has neither a part nor any version).
    * the explicit seam value — the PARENT's baseline genus, resolved by
      the caller: for a v1 on an import the GENUS OF THE STORED, REPAIRED
      PART MESH (``{repo}/versions/{v1}/part.stl`` — the mesh the render
      imports, never the report's ``hole_count``, which overstates the
      baseline whenever the import had open gaps), or the measured genus
      of the parent version's rendered mesh for v2+ edits (the seam
      measures the version's rendered STL). A missing/unreadable mesh is
      passed as ``-1`` (the unknown sentinel).
    * ``None`` (the check abstains) when the explicit value is corrupt —
      a fabricated baseline would make the gate lie. A non-integer (or
      bool) or negative count (the ``-1`` unknown sentinel) abstains the
      same way :func:`d33d.part_holes.part_has_hole_evidence` degrades
      it.

    Issue #418: since the parent baseline applies to every v2+ edit
    (designed or imported), the ``None``-in → ``0`` path is hit only by
    a genuinely fresh design (no part, no version) — the seam no longer
    omits the kwarg for a part-less project that has a rendered version.
    """
    if through_baseline_genus is None:
        return 0
    if isinstance(through_baseline_genus, bool) or not isinstance(
        through_baseline_genus, int
    ):
        return None
    if through_baseline_genus < 0:
        return None
    return through_baseline_genus


def route_through_hole_repair(
    request: str,
    stl: str | None,
    through_baseline_genus: BaselineGenus | None,
    scad_source: str,
    through_baseline_genus_source: str | None = None,
) -> tuple[str, str] | None:
    """Run the through-hole post-check and, on a miss, route the repair.

    The ok-render-branch seam the design loop calls (off the event loop):
    resolves the baseline, runs :func:`through_hole_check`, and on a
    detection tuple ``(baseline, genus)`` routes the EXISTING
    ``geometrically_wrong`` class through ``route_repair`` (no new class,
    no new error_class) — the #317 screw-hole pattern. Returns
    ``(evidence, directive.instruction)`` on a routed repair, ``None``
    when the check abstains or the hole passed (the candidate proceeds).

    A ``route_repair`` directive of ``None`` (unrepairable — the class is
    repairable by contract, so only a routing failure) also yields
    ``None``: the candidate proceeds rather than crashing the loop.

    ``through_baseline_genus_source`` (issue #418) is the adapter seam's
    provenance string for the baseline (e.g. "parent version v107 rendered
    genus: 1"). It is forwarded to :func:`through_hole_check` which logs
    it with every decision, so QA can see WHERE the baseline came from.
    """
    from d33d.failure_classes import ClassifiedFailure, route_repair

    baseline = resolve_baseline_genus(through_baseline_genus)
    det = through_hole_check(
        request, stl, baseline, through_baseline_genus_source
    )
    if det is None:
        return None
    base_g, genus = det
    if _request_is_existing_hole(request):
        # Issue #418: an existing-hole request fails only when the genus
        # FELL below the baseline — a hole that disappeared / never
        # re-opened.
        evidence = (
            f"rendered mesh genus {genus} fell below the "
            f"baseline genus {base_g} — the hole no longer passes "
            f"through"
        )
    else:
        evidence = (
            f"rendered mesh genus {genus} does not exceed the "
            f"baseline genus {base_g} — the hole did not pass through"
        )
    classified = ClassifiedFailure(
        failure_class="geometrically_wrong",
        evidence=evidence,
        repairable=True,
    )
    directive = route_repair(classified=classified, scad_source=scad_source)
    if directive is None:
        logger.warning(
            "through-hole check detected (baseline genus %d, source: %s, rendered genus %d) "
            "but route_repair returned no directive — no repair routed",
            base_g,
            through_baseline_genus_source or "not set by the seam",
            genus,
        )
        return None
    return (evidence, _instruction_with_span(THROUGH_HOLE_INSTRUCTION, stl))


def _instruction_with_span(instruction: str, stl: str | None) -> str:
    """Issue #432: append the measured part z-span to the repair instruction,
    so the next attempt cuts a solid that reaches past both faces by a known
    margin. Abstains to the bare instruction when the STL cannot be read."""
    if not isinstance(stl, str) or not stl:
        return instruction
    import trimesh

    try:
        bounds = trimesh.load(stl, process=False, force="mesh").bounds
    except (OSError, ValueError, RuntimeError, TypeError) as exc:
        logger.warning(
            "through-hole span unavailable (stl %s): %r — repair sent without the measured z-span",
            stl,
            exc,
            exc_info=True,
        )
        return instruction
    if bounds is None:
        logger.warning(
            "through-hole span unavailable (stl %s): empty mesh — repair sent without the measured z-span",
            stl,
        )
        return instruction
    zmin, zmax = (float(v) for v in bounds[:, 2])
    thickness = zmax - zmin
    return (
        f"{instruction} Measured: the part is {thickness:g} mm thick "
        f"(z {zmin:g} to {zmax:g} mm); the cutting solid must span z "
        f"{zmin - 1:g} to {zmax + 1:g} mm (1 mm past each face)."
    )


def through_hole_check(
    request: str,
    stl: str | None,
    baseline_genus: BaselineGenus | None,
    baseline_source: str | None = None,
) -> tuple[int, int] | None:
    """The through-hole post-check (issue #386, operator decision
    2026-10-05; issue #418 article rule).

    Runs on the ok-render branch of the loop, BEFORE the ``score.perfect``
    pass return (the loop calls :func:`route_through_hole_repair` — the
    thin wrapper that resolves the baseline and routes the repair).
    Returns ``None`` (abstain — nothing is fed to the next
    iteration) unless ALL of the following hold:

    * the ``request`` (the current user message, exactly as the loop
      receives it) asks for a through-hole (:func:`is_through_request`);
    * ``stl`` loads and splits with at least one watertight component;
    * ``baseline_genus`` is known (``None`` → abstain).

    The comparison is article-conditional (issue #418):

    * a NEW-hole request (indefinite article or a stated count — "drill
      a hole through", "add two holes through") requires the rendered
      mesh's total genus to RISE by the requested count above the
      baseline: ``genus >= baseline + count`` → ``None`` (pass);
      otherwise the DETECTION tuple ``(baseline, genus)`` — a pocket over
      a part whose own holes already matched the render means the new
      hole did not pass through.
    * an EXISTING-hole request (definite article — "the through-hole",
      "make the hole 8 mm", "move the through-hole") requires the genus
      not to FALL below the baseline: ``genus >= baseline`` → ``None``
      (pass — an unchanged genus passes, a resize or move that preserves
      the hole); ``genus < baseline`` → the DETECTION tuple (a hole that
      closed).

    ``baseline_source`` (issue #418) is the adapter seam's provenance
    string for the baseline — it is logged with every decision (both
    pass and fail) so QA can see WHERE the baseline came from, not just
    the number; ``None`` names the raw value only.

    The caller (``d33d.design_loop``) folds a detection tuple into the
    repair dict — the EXISTING ``geometrically_wrong`` class, no new
    class — and routes it through ``route_repair``.
    """
    if not is_through_request(request):
        return None
    _src = baseline_source or "not set by the seam"
    if baseline_genus is None:
        logger.info(
            "through-hole check abstained: no baseline genus (source: %s)",
            _src,
        )
        return None
    if not isinstance(stl, str) or not stl:
        logger.info(
            "through-hole check abstained: render carries no STL path "
            "(baseline %s, source: %s)",
            baseline_genus,
            _src,
        )
        return None
    genus = _rendered_genus(stl)
    if genus is None:
        logger.info(
            "through-hole check abstained: rendered genus could not be "
            "measured (baseline %s, source: %s)",
            baseline_genus,
            _src,
        )
        return None
    if _request_is_existing_hole(request):
        # Issue #418: existing hole — unchanged genus passes; only a
        # fall below the baseline (a hole that closed) fails.
        _verdict = "pass" if genus >= baseline_genus else "FAIL"
        logger.info(
            "through-hole check (existing hole): rendered genus %d vs "
            "baseline genus %d — %s (source: %s)",
            genus, baseline_genus, _verdict, _src,
        )
        if genus >= baseline_genus:
            return None
        return (baseline_genus, genus)
    count = requested_hole_count(request)
    # Issue #418: new hole(s) — the genus must rise by the requested
    # count above the baseline.
    _verdict = "pass" if genus >= baseline_genus + count else "FAIL"
    logger.info(
        "through-hole check (new hole ×%d): rendered genus %d vs baseline "
        "genus %d — %s (source: %s)",
        count, genus, baseline_genus, _verdict, _src,
    )
    if genus >= baseline_genus + count:
        return None
    return (baseline_genus, genus)
