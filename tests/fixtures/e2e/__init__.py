"""Shared loader for the issue #102 recorded wire fixtures.

One loader, one format: every fixture is a single
``tests/fixtures/e2e/<seam>.json`` carrying ``provenance`` (the commit
recorded at, the date, the model/endpoint where relevant) + the
normalised ``payload`` + the ``expected`` output the replay asserts on.
Binary payloads (STL/PNG) are base64-encoded inside the JSON.

The recorded fixtures were captured by ``scripts/record_seam_fixtures.py``
with a LIVE ``send()`` / ``render_for_design_loop`` run (no Docker; the
LLM edge stubbed to return the recorded wire bytes, the Docker edge
stubbed to plant the committed ``box_20mm.stl`` fixture). ``SEAM A``
records BOTH real production shapes (T0 native tool_call with string
arguments — #80; T0 fenced-JSON-in-content — #79); ``SEAM B`` records
the result.json the worker writes; ``SEAM C`` records a full
``IterationRecord``; ``SEAM D`` records a full frame stream.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from d33d.design_llm import LLMResult
from d33d.render_worker import RenderResult

__all__ = [
    "E2E_DIR",
    "FixtureMissing",
    "SeamFixture",
    "llm_result_from_payload",
    "load_all_fixtures",
    "load_fixture",
]

E2E_DIR = Path(__file__).parent


class FixtureMissing(FileNotFoundError):
    """A fixture that should exist (recorded at ``tests/fixtures/e2e/``)
    is absent — a replay test cannot run against a missing wire fixture,
    and the provenance test must be able to distinguish absent from
    present."""


@dataclass(frozen=True)
class SeamFixture:
    """One loaded wire fixture: provenance + payload + expected output.

    ``provenance`` keys: ``commit`` (the ``git rev-parse HEAD`` at record
    time), ``date`` (ISO date), plus ``model``/``endpoint``/``shapes``
    where relevant. ``normalised`` lists what was normalised away
    (machine-specific absolute paths, uuids) for portability.
    """

    seam: str
    provenance: dict[str, Any]
    payload: dict[str, Any]
    expected: dict[str, Any]

    @property
    def normalised(self) -> list[str]:
        return list(self.provenance.get("normalised", []))


def _b64decode(value: str) -> bytes:
    return base64.b64decode(value.encode("ascii"))


def _b64encode(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


def _rebuild_render_result(payload: dict[str, Any]) -> RenderResult:
    """Rebuild the seam-B ``RenderResult`` from the fixture's payload.

    The payload carries the ``result.json`` wire fields (top-level scalars
    + nested ``artifacts`` dict) plus ``render_artifact_dir`` (the caller's
    persistent path — it is on the returned object, not in result.json).
    The REAL deserialiser (``RenderResult.from_dict``) parses the wire
    fields; the frozen dataclass is rebuilt (never mutated) to carry
    ``render_artifact_dir``."""
    artifacts = payload.get("artifacts") or {}
    wire = {
        "ok": payload["ok"],
        "exit_code": payload["exit_code"],
        "duration_ms": payload["duration_ms"],
        "error_class": payload["error_class"],
        "stderr": payload.get("stderr", ""),
        "artifacts": artifacts,
    }
    result = RenderResult.from_dict(wire)
    # ``render_artifact_dir`` is not a result.json field — it is the
    # caller's persistent path. Rebuild the frozen dataclass to carry it.
    if payload.get("render_artifact_dir") is not None:
        result = RenderResult(
            ok=result.ok,
            exit_code=result.exit_code,
            duration_ms=result.duration_ms,
            error_class=result.error_class,
            stderr=result.stderr,
            stl=result.stl,
            csg=result.csg,
            views=result.views,
            render_artifact_dir=payload["render_artifact_dir"],
        )
    return result


def load_fixture(seam: str) -> SeamFixture:
    """Load ``tests/fixtures/e2e/<seam>.json`` (the single loader both the
    replay tests and the provenance test consume).

    The returned ``payload``/``expected`` dicts are the normalised
    payload — binary fields are STILL base64-encoded strings (callers
    decode via :func:`b64_decode` / the seam-specific rebuilders), so the
    JSON on disk and the in-memory fixture agree byte-for-byte.
    """
    path = E2E_DIR / f"{seam}.json"
    if not path.is_file():
        raise FixtureMissing(f"wire fixture missing: {path} (record via scripts/record_seam_fixtures.py --seam {seam})")
    data = json.loads(path.read_text(encoding="utf-8"))
    return SeamFixture(
        seam=seam,
        provenance=data["provenance"],
        payload=data["payload"],
        expected=data["expected"],
    )


def load_all_fixtures() -> list[SeamFixture]:
    """Every committed wire fixture (the provenance test walks this)."""
    out = []
    for path in sorted(E2E_DIR.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        out.append(
            SeamFixture(
                seam=path.stem,
                provenance=data["provenance"],
                payload=data["payload"],
                expected=data["expected"],
            )
        )
    return out


def llm_result_from_payload(payload: dict[str, Any]) -> LLMResult:
    """Rebuild the seam-A ``LLMResult`` from the fixture payload (the
    normalised ``send()`` return: ``content`` / ``tool_calls`` (list of
    {name, arguments-as-dict}) / ``prompt_hash`` / ``tier`` / ``status``
    / ``request_body`` / ``usage``)."""
    return LLMResult(
        content=payload["content"],
        tool_calls=tuple(payload["tool_calls"]),
        prompt_hash=payload["prompt_hash"],
        tier=payload["tier"],
        status=payload["status"],
        request_body=payload["request_body"],
        usage=dict(payload.get("usage") or {}),
    )
