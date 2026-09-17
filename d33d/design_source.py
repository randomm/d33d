"""The per-version design SCAD source: server-side write, retrieval, prompt
section (issue #105).

The design assistant starts every conversational turn blind unless the prompt
carries the design it previously produced: turn 1 "a sphere 3cm in diameter"
correctly emits ``diameter = 30``, but turn 2 "add another sphere underneath,
3cm" invents ``diameter = 60`` because the prompt never described the existing
geometry. The fix is three small, server-owned pieces (PM decisions, settled):

* **The SERVER writes the source** (not the SPA): a design that passes the
  loop is persisted by the server in the same code path that creates the
  version — a design that passes the gates can never silently fail to
  persist because the client never called the (unused)
  ``putDesignSource`` route (the #91 defect: a gate depended on data the
  browser never sent).
* **The source is PER-VERSION**: a version owns its geometry. The source is
  stored as ``versions/{id}/design.scad`` in the per-project git repo,
  written and committed in the SAME commit as ``versions/{id}/params.json``
  (one writer, one lock, one commit — the smallest durable mechanism
  consistent with the existing architecture; no new table, no blob).
* **The prompt carries the CURRENT design's source**, where "current" is
  the version the project's ``current_version`` pointer points at. Both
  restore paths (``restore_version``'s forward version, ``set_as_main``'s
  pointer move) move that pointer — so retrieval follows a restore by
  construction, no special-casing.

Prompt-size policy (PM decision 3): the source is carried IN FULL, bounded
by the render worker's ``MAX_SCAD_SOURCE_BYTES`` (256 KiB — the bound the
worker will actually accept; a source larger than that cannot be rendered,
so carrying more is incoherent). A source past the bound is truncated with
a marker VISIBLE IN THE PROMPT TEXT — a silent truncation that removes the
module the user is referring to is the worst outcome, so the marker names
the byte count and states plainly that the source is incomplete.

The prompt section sits BETWEEN the #120 design-state block (the
previous version's parameters, #120's block) and the ``Reference
dimensions`` line (the same insertion point the state block uses). It is
labelled ``Current design source (OpenSCAD)`` — unmistakably different from
the REPAIR block's ``previous_scad:`` (the failed candidate from iteration N
of THIS turn, never the accepted design) — and renders explicit
"no existing design yet" wording when no version owns a source (an absent
section is indistinguishable from a bug). The region-edit route reuses this
exact section (PM decision 4: one mechanism, one wording).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from d33d.design_loop import MAX_SCAD_SOURCE_BYTES

#: ``versions/{id}/design.scad`` — the per-version source file (the
#: version's params.json sibling; both land in the same commit).
SOURCE_FILENAME = "design.scad"

#: The visible marker appended when a carried source exceeds
#: :data:`MAX_SCAD_SOURCE_BYTES` (never a silent truncation — the model
#: must know it is seeing a partial file).
TRUNCATION_MARKER = (
    "\n... TRUNCATED: the design source exceeds the 256 KiB byte limit; "
    "this prompt carries only the first {kept} bytes. The source shown "
    "here is INCOMPLETE — the full file is longer and contains further "
    "modules beyond this point."
)


def source_path_for_version(repo_dir: Path, version_id: int) -> Path:
    """The per-version source path: ``repo_dir/versions/{id}/design.scad``."""
    return Path(repo_dir) / "versions" / str(version_id) / SOURCE_FILENAME


def current_version_source(
    row: dict[str, Any], versions_svc: Any
) -> str | None:
    """The CURRENT design's SCAD source, or ``None`` (turn one / a project
    whose current version predates per-version sources).

    "Current" is pinned: the project's ``current_version`` pointer.
    ``restore_version`` creates a forward version (whose create path
    advances the pointer) and ``set_as_main`` re-points it in place — both
    restore paths move this pointer, so retrieval follows a restore by
    construction. A version with no source file (a pre-#105 version, or a
    manual create without geometry) yields ``None`` — the prompt renders
    the explicit no-design wording rather than inventing a source.
    """
    current = row.get("current_version")
    if current is None:
        return None
    path = source_path_for_version(Path(row["git_repo_path"]), int(current))
    if not path.is_file():
        return None
    return path.read_text(encoding="utf-8")


def _truncate_utf8(text: str, limit: int) -> tuple[str, int]:
    """``text`` truncated to at most ``limit`` UTF-8 bytes (never splitting
    a multi-byte character), returning ``(truncated, kept_bytes)``.

    A hard byte cut could land inside a multi-byte sequence and produce an
    invalid UTF-8 prompt; the cut is therefore re-encoded to find the
    exact byte length of the kept prefix (``len(text[:i])`` counts
    characters, not bytes).
    """
    data = text.encode("utf-8")
    if len(data) <= limit:
        return text, len(data)
    cut = data[:limit]
    kept = cut.decode("utf-8", "ignore")
    return kept, len(kept.encode("utf-8"))


def _carry_limit() -> int:
    """The byte budget the carried source gets INSIDE the prompt (the
    marker text has to fit in the same budget — a truncated source plus
    marker must itself stay within :data:`MAX_SCAD_SOURCE_BYTES`)."""
    return MAX_SCAD_SOURCE_BYTES - len(TRUNCATION_MARKER.encode("utf-8"))


def design_source_lines(source: str | None) -> list[str]:
    """The prompt section for the current design source (the labelled lines
    ``_design_messages`` inserts beside the #120 state block).

    - ``source is None``: the explicit clean-slate wording (never a
      silently-absent section — an absent section is indistinguishable
      from a bug, and the wording must not read as an instruction to
      produce nothing).
    - a source within :data:`MAX_SCAD_SOURCE_BYTES`: the full source,
      verbatim (the header line names the section; the source body is
      separate lines — every original line, plus the header).
    - a source past the bound: the source truncated to the carry budget
      plus the VISIBLE marker naming the bound and the bytes carried.

    The header line is always the fixed ``Current design source (OpenSCAD)``
    (the tests pin it as a substring; it carries no inline colon-prefixed
    value — the clean-slate and carry cases each emit their own body line).
    """
    HEADER = "Current design source (OpenSCAD):"
    if source is None:
        return [
            HEADER
            + " NONE — no existing design yet; this is a fresh start, "
            "so CREATE a complete new design (do not reply empty; the "
            "absence of a prior design is not a reason to produce none).",
        ]
    if len(source.encode("utf-8")) > MAX_SCAD_SOURCE_BYTES:
        kept, kept_bytes = _truncate_utf8(source, _carry_limit())
        marker = TRUNCATION_MARKER.format(kept=kept_bytes)
        return [
            HEADER
            + " the accepted design from the previous turn (TRUNCATED — "
            "see the marker at the end of the source below). MODIFY this "
            "design and reply with the COMPLETE updated source:",
            kept + marker,
        ]
    return [
        HEADER
        + " the accepted design from the previous turn. MODIFY this design "
        "and reply with the COMPLETE updated source:",
        source,
    ]


def store_version_source(
    repo_dir: Path, version_id: int, source: str
) -> None:
    """Write ``versions/{id}/design.scad`` for ``version_id``.

    Called INSIDE the version-write lock, after ``params.json`` is written
    but BEFORE the version commit: both files then land in the SAME commit
    (one writer, one lock, atomic with respect to readers — a reader can
    never see a version row whose geometry is missing from a passing loop
    that produced one). Callers that fail before the commit should let
    the version-creation rollback path remove the whole
    ``versions/{id}/`` directory (the row and both files fall back
    together).
    """
    from d33d.versions import install_text_file_atomic

    install_text_file_atomic(source_path_for_version(repo_dir, version_id), source)


__all__ = [
    "SOURCE_FILENAME",
    "TRUNCATION_MARKER",
    "current_version_source",
    "design_source_lines",
    "source_path_for_version",
    "store_version_source",
]
