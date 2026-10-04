"""Fast-layer test: suppression ratchet (issue #354).

Counts lines carrying a lint/type suppression marker across ``d33d/``,
``tests/`` and ``web/src/`` and asserts the total does not rise above a
recorded baseline. The operator decision (issue #354) is that existing
suppressions are NOT bulk-rewritten — they are removed opportunistically —
so this guard ratchets the count down and never lets it up.

Counting rules (they match the baseline measurement):
- one line per matching source line, regardless of how many tokens it
  carries (a line with both ``noqa`` and ``type: ignore`` counts once);
- a line that merely MENTIONS a token in a comment/docstring (e.g. a
  regression note about a prior noqa sweep) counts too — the ratchet is a
  blunt line-count, and a future real suppression could hide behind such a
  line. The baseline was measured with the same rule;
- ``__pycache__`` and binary files are excluded;
- this file excludes itself (its docstring and token literals would
  otherwise count against the baseline).
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

SCAN_ROOTS = ("d33d", "tests", "web/src")
EXCLUDED_DIRS = {"__pycache__", ".git", "node_modules", "dist", "build", "coverage"}
EXCLUDED_SUFFIXES = {".pyc", ".pyo", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".3mf", ".stl", ".wasm"}

# The five marker families from the issue acceptance criterion.
# The word-boundaried tokens (the bare lint-skip marker, the TypeScript
# one) only trip when they stand alone; the spaced forms are literal
# substrings.
SUPPRESSION_RE = re.compile(
    r"\bnoqa\b|\bts-expect-error\b|\bts-ignore\b"
    r"|type:\s*ignore"
    r"|eslint-disable"
    r"|biome-ignore"
)

# The consolidated #354 branch measured at the commit that removed the two
# conftest ``# noqa: F401`` lines (workstream a) and dropped the redundant
# per-category ratchet of workstream a in favour of this single five-token
# total: 52 lines across 25 files. Ratchet: lower this as suppressions are
# removed opportunistically; never raise it without re-measuring and an
# explicit operator decision.
BASELINE = 52

SELF = Path(__file__).resolve()


def _countable_paths() -> list[Path]:
    paths: list[Path] = []
    for root_name in SCAN_ROOTS:
        root = REPO_ROOT / root_name
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            if path == SELF:
                continue
            if path.suffix in EXCLUDED_SUFFIXES:
                continue
            if any(part in EXCLUDED_DIRS for part in path.relative_to(REPO_ROOT).parts):
                continue
            paths.append(path)
    return paths


def _suppressed_lines(paths: list[Path]) -> list[str]:
    locations: list[str] = []
    for path in paths:
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue  # binary or unreadable: not countable source
        for lineno, line in enumerate(text.splitlines(), start=1):
            if SUPPRESSION_RE.search(line):
                locations.append(f"{path.relative_to(REPO_ROOT)}:{lineno}")
    return locations


def test_suppression_count_does_not_exceed_baseline() -> None:
    locations = _suppressed_lines(_countable_paths())
    assert len(locations) <= BASELINE, (
        f"suppression ratchet: {len(locations)} suppressed lines found, "
        f"baseline is {BASELINE}. Add a suppression only by lowering this "
        f"test's baseline together with an explicit justification "
        f"(issue #354 ratchet). Offending lines:\n"
        + "\n".join(locations)
    )
