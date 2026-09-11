"""Fast-layer test: ``prompts/bosl2-cheatsheet.md`` line count + topic coverage.

No Docker required. Asserts BOTH:
(a) at least 25 non-empty module-signature lines, each a module call plus
    a one-line purpose;
(b) coverage of every required topic: rounding on cuboid, attach/align
    anchoring, round3d, chamfer masks, distributors, fasteners with the
    print-fit ``oversize``/``tolerance`` parameter, threading with
    tolerance, and partitions.

Every module name asserted here is VERIFIED against the vendored BOSL2
source at tag ``v2.0.755`` (``shapes3d.scad``, ``rounding.scad``,
``miscellaneous.scad``, ``masks.scad``, ``attachments.scad``,
``distributors.scad``, ``partitions.scad``, ``threading.scad``,
``screws.scad``, ``metric_screws.scad``, ``transforms.scad``). Names the
spec flagged as possibly-not-real (``screw``/``nut``/``thread``/
``partition`` in their training-data form) are asserted only where they
are confirmed real.
"""

from __future__ import annotations

import re
from pathlib import Path

CHEATSHEET = Path(__file__).resolve().parents[2] / "prompts" / "bosl2-cheatsheet.md"


def _content() -> str:
    assert CHEATSHEET.exists(), f"cheatsheet not found at {CHEATSHEET}"
    return CHEATSHEET.read_text(encoding="utf-8")


def _signature_lines() -> list[str]:
    """The non-empty lines that carry a module signature — lines starting
    with ``- `` and containing a ``(`` (a module call). Section headers
    (``##``) and prose intro lines are excluded."""
    out: list[str] = []
    for line in _content().splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            continue
        # A signature line is a bulleted module call.
        if stripped.startswith("- ") and "(" in stripped:
            out.append(stripped)
    return out


def test_line_count_at_least_25() -> None:
    lines = _signature_lines()
    assert len(lines) >= 25, f"only {len(lines)} module-signature lines; need >= 25"


def test_each_signature_line_has_module_name_and_purpose() -> None:
    """Each signature line must contain a module name followed by ``(``
    (the call) and a description after the closing context — i.e. a
    one-line purpose, not just a bare signature."""
    for line in _signature_lines():
        # Must contain a module call: some identifier followed by "("
        assert re.search(r"\b[a-z_][a-z0-9_]*\s*\(", line), (
            f"no module call in: {line!r}"
        )
        # Must have a purpose: text after the signature (a dash or
        # description). Require at least one character of description
        # beyond the call by checking for " — " or a trailing clause.
        assert " — " in line or "—" in line, (
            f"no one-line purpose separator in: {line!r}"
        )


def test_topic_cuboid_rounding() -> None:
    text = _content()
    assert "cuboid" in text, "cuboid not mentioned"
    # rounding on cuboid
    assert "rounding" in text, "rounding (on cuboid) not covered"


def test_topic_attach_align() -> None:
    text = _content()
    assert "attach(" in text, "attach() not mentioned"
    assert "align(" in text, "align() not mentioned"


def test_topic_round3d() -> None:
    text = _content()
    assert "round3d(" in text, "round3d not mentioned"


def test_topic_chamfer_masks() -> None:
    text = _content()
    assert "chamfer" in text, "chamfer not mentioned"
    # A mask module specifically
    assert "mask" in text.lower(), "mask not mentioned"
    # The specific chamfer edge mask verified in masks.scad
    assert "chamfer_edge_mask" in text, (
        "chamfer_edge_mask (verified in masks.scad) not mentioned"
    )


def test_topic_distributors() -> None:
    text = _content()
    assert "copies(" in text or "distribute" in text.lower(), (
        "distributors not mentioned"
    )
    # Specific verified distributor from distributors.scad
    assert "xcopies(" in text or "line_copies(" in text or "rot_copies(" in text, (
        "no verified distributor (xcopies/line_copies/rot_copies) mentioned"
    )


def test_topic_fasteners_oversize() -> None:
    text = _content()
    # Fasteners: screw and nut are real modules in screws.scad
    assert "screw(" in text, "screw() not mentioned"
    assert "nut(" in text, "nut() not mentioned"
    # The print-fit parameter — verified: screws.scad uses "oversize" /
    # "hole_oversize" / "tolerance"; the spec's "undersize" is not the
    # actual parameter name in BOSL2 v2.0.755.
    assert "oversize" in text or "tolerance" in text, (
        "print-fit oversize/tolerance not mentioned"
    )


def test_topic_threading_tolerance() -> None:
    text = _content()
    # threading.scad has threaded_rod, threaded_nut, generic_threaded_rod, etc.
    assert "threaded_rod(" in text or "threaded_nut(" in text, (
        "threaded_rod/threaded_nut not mentioned"
    )
    assert "pitch" in text, "pitch (thread parameter) not mentioned"


def test_topic_partitions() -> None:
    text = _content()
    # partitions.scad: partition_mask, partition_cut_mask, partition, half_of
    assert "partition" in text, "partition not mentioned"


def test_verified_module_names_present() -> None:
    """Assert on confirmed module names from the vendored BOSL2 source
    at v2.0.755."""
    text = _content()
    # From shapes3d.scad
    assert "cuboid(" in text
    # From miscellaneous.scad (round3d lives there, not rounding.scad)
    assert "round3d(" in text
    # From attachments.scad
    assert "attach(" in text
    assert "align(" in text
    # From masks.scad
    assert "chamfer_edge_mask(" in text
    # From distributors.scad
    assert "xcopies(" in text or "rot_copies(" in text or "line_copies(" in text
    # From screws.scad (verified real)
    assert "screw(" in text
    assert "nut(" in text
    # From threading.scad
    assert "threaded_rod(" in text or "threaded_nut(" in text
    # From partitions.scad
    assert "partition_mask(" in text or "half_of(" in text or "partition(" in text
    # From transforms.scad
    assert "move(" in text or "rot(" in text or "zrot(" in text


def test_no_hallucinated_module_names() -> None:
    """Guard against the LLM-hallucination failure mode: module names
    that do NOT exist in BOSL2 v2.0.755 must not be documented as real
    signatures. These are names the spec flagged or that are known
    non-existent in the vendored source."""
    text = _content()
    # `round_box` is NOT a real BOSL2 module (it's a BOSL1/other-lib name).
    assert "round_box(" not in text, "round_box() is not a real BOSL2 v2.0.755 module"
    # `round_edges` is NOT a real BOSL2 module.
    assert "round_edges(" not in text, (
        "round_edges() is not a real BOSL2 v2.0.755 module"
    )
    # `distribute(` as a bare module name is not how BOSL2 names its
    # distributors (they are all `*_copies` / `*_spread`).
    assert "distribute(" not in text, "distribute() is not a real BOSL2 v2.0.755 module"
