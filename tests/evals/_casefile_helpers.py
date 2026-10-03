"""Shared helpers for the eval case-file tests (issue #340 fix round).

The case-file tests build temp case dirs with a real, pinned prompt
file. :func:`write_cases` is the single home for that scaffolding so
the test modules don't each carry a copy.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path


def write_cases(tmp_path: Path, cases: list[dict]) -> Path:
    """Write case files (with a real, pinned prompt file) into a temp dir."""
    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir(exist_ok=True)
    prompt = prompts_dir / "p.md"
    prompt.write_text("# prompt v1", encoding="utf-8")
    pin = hashlib.sha256(prompt.read_bytes()).hexdigest()
    cases_dir = tmp_path / "cases"
    cases_dir.mkdir(exist_ok=True)
    for c in cases:
        doc = {
            "prompt": {
                "prompt_version": "v1",
                "path": str(prompt),
                "sha256": pin,
            },
            "gate_expectations": ["compile"],
        }
        doc.update(c)
        (cases_dir / f"{c['case_id']}.json").write_text(
            json.dumps(doc), encoding="utf-8"
        )
    return cases_dir
