#!/usr/bin/env python3
"""Fold ``evals/failures.jsonl`` lines into golden-set candidates.

Issue #9, workstream task-failures. The ticket's "Growth — evals from
production traces" section: a one-line hook in the renderer appends to
``evals/failures.jsonl`` on any deterministic gate failure IN PRODUCTION
(the pydantic pin is ``d33d/evals/failure_capture.py``); a documented
fold procedure or script ingests those lines into golden cases monthly,
preserving provenance back to the originating failure line.

This script is the fold. It reads every line of ``evals/failures.jsonl``,
groups them by ``failure_class``, and emits one candidate golden case per
group under ``evals/cases/from-failures/`` — each candidate carries the
provenance (``source_event_id`` + ``source_line_number``) back to the
originating failure line, plus the 7-field payload the case needs
(photo, region mark, request, model, prompt version, output scad,
failure class).

Usage::

    python evals/fold_failures.py                    # default paths
    python evals/fold_failures.py --failures PATH    # explicit file
    python evals/fold_failures.py --out DIR          # explicit output dir
    python evals/fold_failures.py --dry-run          # print, don't write

The fold is idempotent: re-running it over the same failures file
regenerates the same candidate files (a case file is a pure function of
its group's lines — the provenance list is sorted by line number). A
case is only ever a CANDIDATE (the operator reviews and promotes it to
the golden set's tracked cases with a stable case id); the fold never
renames or rewrites an existing tracked case.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from d33d.evals.failure_capture import FailureEvent

# Make the repo root importable (the script lives at ``evals/``).
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


def fold(
    failures: list[tuple[int, FailureEvent]],
    out_dir: Path,
    *,
    dry_run: bool = False,
) -> list[Path]:
    """Fold the ``(line_number, event)`` pairs into candidate case files.

    Groups by ``failure_class``; one candidate file per class (a class
    with no lines is not written). Each candidate is a JSON object::

        {
          "provenance": [
            {"event_id": ..., "line": <1-based line number in failures.jsonl>},
            ...
          ],
          "failure_class": "...",
          "count": <n lines in this group>,
          "requests": [...],          # the distinct request strings
          "models": [...],            # the distinct model ids
          "prompt_versions": [...],   # the distinct prompt hashes
          "output_scads": [...],      # the distinct .scad sources
          "photos": [...],            # the distinct photo refs (None filtered)
          "region_marks": [...]       # the distinct region marks (None filtered)
        }

    The candidate file is ``evals/cases/from-failures/<failure_class>.json``.
    Returns the list of paths written (empty on ``dry_run``).
    """
    groups: dict[str, list[tuple[int, FailureEvent]]] = {}
    for line_no, ev in failures:
        groups.setdefault(ev.failure_class, []).append((line_no, ev))

    written: list[Path] = []
    for fc in sorted(groups):
        lines = sorted(groups[fc], key=lambda pair: pair[0])
        payload = {
            "provenance": [
                {"event_id": ev.event_id, "line": ln} for ln, ev in lines
            ],
            "failure_class": fc,
            "count": len(lines),
            "requests": sorted({ev.request for _, ev in lines}),
            "models": sorted({ev.model for _, ev in lines}),
            "prompt_versions": sorted({ev.prompt_version for _, ev in lines}),
            "output_scads": sorted({ev.output_scad for _, ev in lines if ev.output_scad}),
            "photos": sorted({ev.photo for _, ev in lines if ev.photo}),
            "region_marks": sorted({ev.region_mark for _, ev in lines if ev.region_mark}),
        }
        target = out_dir / f"{fc}.json"
        text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        if dry_run:
            print(f"[dry-run] {target}")
            print(text)
            continue
        out_dir.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        written.append(target)
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--failures",
        type=Path,
        default=None,
        help="path to failures.jsonl (default: <repo>/evals/failures.jsonl)",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="output dir for candidate cases (default: <repo>/evals/cases/from-failures)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the candidate files to stdout instead of writing them",
    )
    args = parser.parse_args(argv)

    failures_path = args.failures or (_REPO_ROOT / "evals" / "failures.jsonl")
    out_dir = args.out or (_REPO_ROOT / "evals" / "cases" / "from-failures")

    if not failures_path.exists():
        print(f"no failures file at {failures_path} — nothing to fold", file=sys.stderr)
        return 0

    # Read every line with its 1-based line number (the provenance key).
    raw_lines = failures_path.read_text(encoding="utf-8").splitlines()
    failures: list[tuple[int, FailureEvent]] = []
    for i, raw in enumerate(raw_lines, start=1):
        if not raw.strip():
            continue
        obj = json.loads(raw)
        failures.append((i, FailureEvent.model_validate(obj)))

    written = fold(failures, out_dir, dry_run=args.dry_run)
    if args.dry_run:
        print(f"[dry-run] {len(failures)} lines folded into {len(_group_count(failures))} candidate(s)")
    else:
        print(f"folded {len(failures)} lines into {len(written)} candidate case(s) under {out_dir}")
    return 0


def _group_count(failures: list[tuple[int, FailureEvent]]) -> int:
    return len({ev.failure_class for _, ev in failures})


if __name__ == "__main__":
    raise SystemExit(main())
