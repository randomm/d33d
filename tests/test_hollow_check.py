"""Issue #436: the near-hollow post-check (solid volume < 10% of bbox)."""

from __future__ import annotations

from pathlib import Path

import pytest

from d33d.hollow_check import hollow_solid_check, is_hollow_request

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "stl"
SHELL = str(FIXTURES / "hollow-shell.stl")
SOLID = str(FIXTURES / "solid-block.stl")


def test_near_hollow_stl_fires_for_a_plain_box_request() -> None:
    result = hollow_solid_check("a box", SHELL)
    assert result is not None
    evidence, instruction = result
    assert "solid volume" in evidence
    assert "% of the bounding box" in evidence
    assert instruction


def test_solid_stl_passes() -> None:
    assert hollow_solid_check("a 20 mm block", SOLID) is None


def test_missing_stl_abstains() -> None:
    assert hollow_solid_check("a box", str(FIXTURES / "nope.stl")) is None
    assert hollow_solid_check("a box", None) is None


@pytest.mark.parametrize(
    "request_text",
    [
        "a shell 20 mm across",
        "a thin FRAME for a phone",
        "a ring holder",
        "two rings",
        "a tube 30 mm long",
        "wire clip",
        "a mesh tray",
        "a lattice cube",
        "a wall bracket",
        "a hook",
        "a spring-loaded cap",
        "a hollow cylinder",
        "brackets for a shelf",
        "a cup 50 mm across, 60 mm tall, 1 mm wall",
        "a thin-walled box",
        "an enclosure for a PCB",
        "a bowl",
        "a container with a lid",
        "a tray with 30° sides",
    ],
)
def test_thin_or_hollow_request_is_exempt(request_text: str) -> None:
    assert is_hollow_request(request_text)
    assert hollow_solid_check(request_text, SHELL) is None


@pytest.mark.parametrize(
    "request_text", ["a box", "a lid for a 40 mm jar", "a cupboard door", ""]
)
def test_request_naming_no_thin_form_is_not_exempt(request_text: str) -> None:
    assert not is_hollow_request(request_text)
    assert hollow_solid_check(request_text, SHELL) is not None
