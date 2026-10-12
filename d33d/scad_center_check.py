"""Wrong-typed ``center=[...]`` SCAD-text check (issue #436).

OpenSCAD's ``center`` argument on ``cube()`` / ``cylinder()`` is a single
boolean. A vector ``center=[true, true, true]`` is accepted silently: the
render succeeds, the argument is ignored and the geometry is left
uncentred. OpenSCAD emits no warning, so the render log looks clean and
the stderr path (:func:`d33d.render_diagnostics.unknown_parameters`) never
sees it. This module is the text-level check that catches it.

The finding is returned as structured ``(call, evidence)`` data. The caller
builds the ``unknown_variable`` failure directly from it and routes it through
``route_repair`` (the same downstream as the #419 unknown-parameter path); no
OpenSCAD stderr line is synthesised.

Only ``cube(`` and ``cylinder(`` calls are scanned; comments and string
bodies are stripped first so a ``center=[`` inside them is never flagged.
"""

from __future__ import annotations

import re

__all__ = [
    "CENTER_VECTOR_RE",
    "center_vector_warning",
    "find_primitive_calls",
    "strip_comments_and_strings",
]

#: The primitives whose ``center`` parameter is a boolean.
_CENTER_PRIM_RE = re.compile(r"\b(cube|cylinder)\s*\(")

#: A vector ``center=[...]`` argument (``$center`` / ``xcenter`` excluded).
CENTER_VECTOR_RE = re.compile(r"(?<![\w$])center\s*=\s*\[")


def strip_comments_and_strings(text: str) -> str:
    """Blank out SCAD ``//`` and ``/* */`` comments and string bodies,
    preserving offsets and line structure (characters are replaced with
    spaces, never deleted)."""
    out: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        c = text[i]
        if c == "/" and text[i + 1 : i + 2] == "/":
            j = text.find("\n", i)
            j = n if j == -1 else j
            out.append(" " * (j - i))
            i = j
        elif c == "/" and text[i + 1 : i + 2] == "*":
            j = text.find("*/", i + 2)
            j = n if j == -1 else j + 2
            out.append("".join("\n" if ch == "\n" else " " for ch in text[i:j]))
            i = j
        elif c == '"':
            j = i + 1
            while j < n and text[j] != '"' and text[j] != "\n":
                if text[j] == "\\":
                    j += 1
                j += 1
            j = min(j + 1, n)
            out.append(" " * (j - i))
            i = j
        else:
            out.append(c)
            i += 1
    return "".join(out)


def find_primitive_calls(text: str, prim_re: re.Pattern[str]) -> list[tuple[int, str]]:
    """``(start_line, call_text)`` for each match of ``prim_re`` in ``text``,
    the call joined across line breaks by tracking paren depth to its
    matching ``)``. ``text`` must already be comment/string-stripped."""
    calls: list[tuple[int, str]] = []
    for m in prim_re.finditer(text):
        open_paren = text.index("(", m.start())
        depth = 0
        j = open_paren
        n = len(text)
        while j < n:
            ch = text[j]
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        if depth != 0:
            j = text.find("\n", open_paren)
            j = n if j == -1 else j
        start_line = text.count("\n", 0, m.start()) + 1
        calls.append((start_line, text[m.start() : j + 1]))
    return calls


def center_vector_warning(scad_source: str) -> tuple[str, str] | None:
    """The first wrong-typed ``center=[...]`` call as ``(call, evidence)``,
    or ``None`` when there is none.

    ``call`` is the offending primitive call text; ``evidence`` names the
    ``center`` argument, the primitive and the call.
    """
    stripped = strip_comments_and_strings(scad_source or "")
    hit = next(
        (
            " ".join(call.split())
            for _line, call in find_primitive_calls(stripped, _CENTER_PRIM_RE)
            if CENTER_VECTOR_RE.search(call)
        ),
        None,
    )
    if hit is None:
        return None
    call = hit
    prim = call.split("(", 1)[0].strip()
    evidence = (
        f'center=[...] is a vector on {prim}(); OpenSCAD takes center as a '
        f"boolean and ignores the vector: {call[:160]}"
    )
    return call, evidence
