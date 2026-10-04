"""Ratchet test: suppression counts must not increase beyond the recorded baseline.

Counts suppression lines across d33d/, tests/, and web/src/ (excluding
__pycache__ and this test's own source) and fails when any category exceeds
its baseline. "Multi-token lines" are handled: if a single source line carries
more than one suppression token (e.g. ``# noqa: BLE001, S110``), the line still
counts as one line toward the ``noqa`` baseline — the baseline tracks lines,
not individual tokens.

To bump a baseline intentionally (e.g. after a large refactor that
opportunistically removes suppressions and leaves fewer than the current
baseline), edit the value in the ``BASELINES`` dict below.

The test is intentionally fast: it reads source files from the repo root
and counts matching lines without importing them.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Baselines: (noqa_lines, eslint_disable_lines, type_ignore_lines)
#
# Recorded from the tree at the commit that introduced this test.
# The ratchet allows counts to DECREASE (opportunistic removal) but not
# INCREASE beyond these values.
# ---------------------------------------------------------------------------
BASELINES: dict[str, int] = {
    # Lines containing "# noqa" in .py files under d33d/ and tests/
    "noqa": 7,
    # Lines containing "eslint-disable" in .ts/.tsx files under web/src/
    "eslint_disable": 5,
    # Lines containing "# type: ignore" in .py files under d33d/ and tests/
    "type_ignore": 38,
}

# Paths to scan (relative to the repo root). The ratchet test itself is
# excluded so the test does not count its own source.
SCAN_DIRS = ["d33d", "tests", "web/src"]

# Patterns — each line is counted once per pattern it matches. A line can
# match multiple patterns (e.g. a line with both "# noqa: X" and "# type:
# ignore[Y]" counts for both "noqa" and "type_ignore").
PATTERNS: dict[str, re.Pattern[str]] = {
    "noqa": re.compile(r"# noqa"),
    "eslint_disable": re.compile(r"eslint-disable"),
    "type_ignore": re.compile(r"# type: ignore"),
}

# File extensions per scan dir
_PY_EXTENSIONS = {".py"}
_TS_EXTENSIONS = {".ts", ".tsx"}


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _files_to_scan(repo_root: Path) -> list[Path]:
    """Yield all scannable source files, excluding __pycache__ and this test."""
    own_path = Path(__file__).resolve()
    result: list[Path] = []
    for dir_name in SCAN_DIRS:
        scan_dir = repo_root / dir_name
        if not scan_dir.is_dir():
            continue
        for f in scan_dir.rglob("*"):
            if not f.is_file():
                continue
            # Exclude __pycache__ anywhere in the path
            if "__pycache__" in f.parts:
                continue
            # Exclude this test file itself
            if f == own_path:
                continue
            # Only scan relevant extensions
            if f.suffix in _PY_EXTENSIONS:
                # Python files: scan for noqa and type_ignore
                result.append(f)
            elif f.suffix in _TS_EXTENSIONS:
                # TS/TSX files: scan for eslint_disable only
                result.append(f)
    return result


def _count_suppressions(files: list[Path]) -> dict[str, int]:
    """Count suppression lines per category across all given files.

    Each file is scanned for the patterns that apply to its extension.
    A line matching multiple patterns counts for each.
    """
    counts = {key: 0 for key in PATTERNS}

    for f in files:
        try:
            text = f.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue

        is_ts = f.suffix in _TS_EXTENSIONS

        for line in text.splitlines():
            if is_ts:
                # Only count eslint-disable in TS/TSX files
                if PATTERNS["eslint_disable"].search(line):
                    counts["eslint_disable"] += 1
            else:
                # Python files: count noqa and type_ignore
                if PATTERNS["noqa"].search(line):
                    counts["noqa"] += 1
                if PATTERNS["type_ignore"].search(line):
                    counts["type_ignore"] += 1

    return counts


@pytest.mark.parametrize(
    "category",
    ["noqa", "eslint_disable", "type_ignore"],
    ids=["noqa", "eslint_disable", "type_ignore"],
)
def test_suppression_ratchet(category: str) -> None:
    """Fail when a suppression category exceeds its recorded baseline.

    The ratchet only checks that counts do not INCREASE. If a prior cleanup
    reduced a count below the baseline, that is fine — the baseline remains
    the ceiling until explicitly lowered.
    """
    repo_root = _repo_root()
    files = _files_to_scan(repo_root)
    counts = _count_suppressions(files)

    baseline = BASELINES[category]
    actual = counts[category]

    assert actual <= baseline, (
        f"Suppression ratchet: {category!r} count is {actual}, "
        f"baseline is {baseline}. "
        f"Remove the new suppression or lower the baseline if this is "
        f"intentional (see tests/test_suppression_ratchet.py)."
    )
