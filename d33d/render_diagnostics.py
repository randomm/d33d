"""OpenSCAD diagnostic-text helpers shared by the render worker and the
failure classifier (issue #383).

Both the render worker (``d33d.render_worker.classify`` — the ``ok`` /
``syntax_error`` decision) and the LLM failure classifier
(``d33d.failure_classes._classify_syntax_error`` — the named repair class)
must recognise OpenSCAD's undefined-variable warning, so the pattern lives
here once. The two call sites stay thin: each calls :func:`unknown_variables`
and acts on the result.
"""

from __future__ import annotations

import re

#: OpenSCAD's undefined-variable warning (issue #383, captured verbatim
#: from the pinned image ``d33d/render-worker:local`` / OpenSCAD 2026.01.19 —
#: a real render of ``cube(H);`` emits, in ``/work/render.log``:
#: ``WARNING: Ignoring unknown variable "H" in file model.scad, line 1``
#: with exit code 0 and a valid, non-degenerate STL — the entrypoint runs
#: openscad from ``/work``, so the log carries the RELATIVE filename
#: ``model.scad`` and a comma before ``line``). The render compiles
#: "successfully" but the geometry silently fell back to zero-sized shapes
#: — the 17.7 mm³ garbage fragment the ticket describes. The pattern anchors
#: on the version-stable core (``Ignoring unknown variable "..."``): a real
#: capture (issue #383, binding decision 1) shows the trailing file/line
#: shape is image-dependent (relative vs absolute path, comma placement),
#: so it is deliberately NOT part of the match. ``-D`` defines are applied
#: before the source is parsed, so a legitimately ``-D``-defined variable
#: never triggers this line; a variable defined in the file itself is simply
#: not "unknown". The quoted name makes the pattern unambiguous against
#: unrelated text that merely mentions "unknown variable".
UNKNOWN_VARIABLE_RE = re.compile(r'Ignoring unknown variable "(\w+)"')


def unknown_variables(text: str) -> list[str]:
    """The variable names an OpenSCAD unknown-variable warning in ``text``
    names, in first-seen order (deduplicated).

    Empty list when ``text`` carries no such warning (the normal case —
    the caller then falls through to the existing classification).
    """
    seen: list[str] = []
    for name in UNKNOWN_VARIABLE_RE.findall(text or ""):
        if name not in seen:
            seen.append(name)
    return seen
