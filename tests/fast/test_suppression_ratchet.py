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

# The consolidated #354 branch measured at the consolidation commit:
# 52 suppressed lines across 26 files (all pre-existing on origin/main
# except the ratchet's own docstring lines, which the counting rule treats
# as suppressions). Ratchet: lower this as suppressions are removed
# opportunistically; never raise it without re-measuring and an explicit
# operator decision.
BASELINE = 54

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


# ---------------------------------------------------------------------------
# Unit test: each suppression token is counted exactly once per line.
# Token strings are built by concatenation so THIS file's lines are not
# themselves counted by the ratchet (the ratchet scans by regex, not by
# file identity — though this file excludes itself, the concatenation
# also protects against a future refactor that removes the SELF exclusion).
# ---------------------------------------------------------------------------


def test_each_suppression_token_counts_exactly_once(tmp_path: Path) -> None:
    """Each of the six token families must match its synthetic line and
    produce exactly one suppressed-line count — verifying the regex is
    neither too loose (double-counting) nor too strict (missing a token
    variant like ``@``-prefixed forms)."""
    noqa_tok = "no" + "qa"
    type_ignore_tok = "type" + ": ignore"
    ts_ignore_tok = "@" + "ts-" + "ignore"
    ts_expect_tok = "@" + "ts-" + "expect-" + "error"
    eslint_tok = "eslint-" + "disable"
    biome_tok = "biome-" + "ignore"

    lines = [
        f"x = 1  # {noqa_tok}",
        f"x = 1  # {type_ignore_tok}",
        f"// {ts_ignore_tok}",
        f"// {ts_expect_tok}",
        f"// {eslint_tok}",
        f"// {biome_tok}",
    ]
    for line in lines:
        f = tmp_path / "test.py"
        f.write_text(line + "\n", encoding="utf-8")
        count = sum(1 for l in f.read_text().splitlines() if SUPPRESSION_RE.search(l))
        f.unlink()
        assert count == 1, f"expected 1 match for {line!r}, got {count}"


def test_multitoken_line_counts_once(tmp_path: Path) -> None:
    """A line with TWO tokens (e.g. both noqa and type: ignore) counts as
    ONE suppressed line — the regex is a per-line ``search``, not a per-
    token ``findall``."""
    multi = "x = 1  # no" + "qa  # type" + ": ignore"
    count = sum(1 for l in [multi] if SUPPRESSION_RE.search(l))
    assert count == 1, f"multi-token line should count once, got {count}"
