"""tests/test_through_hole_check.py — the through-hole genus post-check
(issue #386, operator decision 2026-10-05).

Unit tests for the trigger word set in ``d33d.through_hole_check``
("through" + a hole noun, case-insensitive whole word; the hyphenated
``through-hole`` / ``thru-hole`` tokens fire; "blind hole", "pocket",
"counterbore 5 mm deep" without "through" never trigger; "throughput",
"thorough", "throughout" never trigger) and for the check's
baseline-exceeding contract (genus must EXCEED the baseline — 0 for a
new design, the imported part's own hole count on an import — using
real tiny STLs built with trimesh in ``tmp_path``).
"""

from __future__ import annotations

from pathlib import Path

import trimesh

from d33d.through_hole_check import (
    is_through_request,
    requested_hole_count,
    through_hole_check,
)

#: The committed STL fixtures (generated once locally with trimesh boolean
#: ops — CI has no boolean backend, so the tests load these instead).
_FIXTURE_DIR = Path(__file__).parent / "fixtures" / "stl"


def _load_fixture(name: str) -> trimesh.Trimesh:
    """Load a committed STL fixture and assert its topology at load time
    (the CI proof — a corrupt fixture fails loudly, not silently)."""
    mesh = trimesh.load(str(_FIXTURE_DIR / name), process=False)
    if isinstance(mesh, trimesh.Scene):
        mesh = mesh.to_mesh()
    mesh.merge_vertices()
    mesh.update_faces(mesh.nondegenerate_faces())
    return mesh


def _pocket_stl() -> trimesh.Trimesh:
    """A 20×20×20 box with a blind pocket (genus 0)."""
    m = _load_fixture("through_hole_pocket.stl")
    assert m.is_watertight, "pocket fixture is not watertight"
    from d33d.part_holes import watertight_genus
    assert watertight_genus(m.split(only_watertight=False)) == 0
    return m


def _through_stl() -> trimesh.Trimesh:
    """A 20×20×20 box with one through-hole (genus 1)."""
    m = _load_fixture("through_hole_genus1.stl")
    assert m.is_watertight, "genus-1 fixture is not watertight"
    from d33d.part_holes import watertight_genus
    assert watertight_genus(m.split(only_watertight=False)) == 1
    return m


def _genus_n_stl(n: int) -> trimesh.Trimesh:
    """A 20×20×20 box with ``n`` through-holes (genus ``n``)."""
    m = _load_fixture(f"through_hole_genus{n}.stl")
    from d33d.part_holes import watertight_genus
    assert watertight_genus(m.split(only_watertight=False)) == n
    return m

# ---------------------------------------------------------------------------
# The trigger word set
# ---------------------------------------------------------------------------


def test_trigger_through_plus_hole_nouns():
    """``through`` + any HOLE_NOUNS member fires (case-insensitive,
    whole word)."""
    for noun in ("hole", "holes", "bore", "counterbore"):
        assert is_through_request(f"drill a 6 mm {noun} through the plate")
        assert is_through_request(f"Drill a 6 mm {noun} THROUGH the plate")


def test_trigger_hyphenated_tokens():
    """The hyphenated tokens fire on their own (no hole noun needed)."""
    assert is_through_request("a 60 × 45 mm plate with a 6 mm through-hole")
    assert is_through_request("add a THRU-HOLE in the centre")
    assert is_through_request("make it a Through-Hole please")


def test_trigger_through_alone_never():
    """``through`` alone (no hole noun, no hyphenated token) never
    triggers — ``throughput``, ``thorough``, ``throughout`` must not
    fire (whole-word matching)."""
    for text in (
        "improve the throughput of the pipeline",
        "be thorough with the fillets",
        "check throughout the whole part",
        "make the wall thicker",
        "a pocket in the middle",
    ):
        assert not is_through_request(text)


def test_trigger_noun_without_through_never():
    """A hole noun without ``through`` never triggers: ``blind hole``,
    ``pocket``, ``counterbore 5 mm deep`` — the trigger requires
    ``through`` (or the hyphenated token)."""
    for text in (
        "drill a 5 mm blind hole 3 mm deep",
        "a pocket 2 mm deep in the top face",
        "a counterbore 5 mm deep for the screw",
        "a 6 mm hole in the corner",
    ):
        assert not is_through_request(text)


def test_trigger_empty_request_never():
    """A blank request abstains (no trigger)."""
    assert not is_through_request("")
    assert not is_through_request("   ")


# ---------------------------------------------------------------------------
# The check contract (baseline comparison, abstain cases)
# ---------------------------------------------------------------------------


def _write_stl(path, mesh: trimesh.Trimesh) -> str:
    mesh.export(str(path))
    return str(path)


def test_pocket_mesh_has_genus_zero_and_through_mesh_genus_one():
    """Sanity: the test meshes themselves carry the expected topology —
    a blind pocket (euler 4 → genus 0), a through-hole (euler 0 → genus
    1)."""
    pocket = _pocket_stl()
    through = _through_stl()
    from d33d.part_holes import watertight_genus

    assert watertight_genus(pocket.split(only_watertight=False)) == 0
    assert watertight_genus(through.split(only_watertight=False)) == 1


def test_check_pocket_over_zero_baseline_fires(tmp_path):
    """A through request + a pocket mesh (genus 0) over a zero baseline
    (a new design) → the DETECTION tuple (0, 0) — the candidate fails,
    routed geometrically_wrong by the loop."""
    stl = _write_stl(tmp_path / "pocket.stl", _pocket_stl())
    det = through_hole_check("drill a 6 mm hole through the box", stl, 0)
    assert det == (0, 0)

def test_check_through_mesh_over_zero_baseline_passes(tmp_path):
    """A through request + a true through-hole (genus 1) over a zero
    baseline → ``None`` (the check passes — the hole did pass)."""
    stl = _write_stl(tmp_path / "through.stl", _through_stl())
    assert through_hole_check("drill a 6 mm hole through the box", stl, 0) is None


def test_check_through_request_over_part_baseline(tmp_path):
    """The baseline comparison: a part whose stored hole count is 1
    (baseline 1) — a pocket render (genus 0) does NOT exceed it (fires),
    a through render (genus 1... does NOT exceed 1 — a single hole on a
    one-hole part; the operator rule is EXCEED, so it also fires), and
    a part baseline the render EXCEEDS passes."""
    pocket_stl = _write_stl(tmp_path / "pocket.stl", _pocket_stl())
    through_stl = _write_stl(tmp_path / "through.stl", _through_stl())
    req = "drill a 6 mm hole through the box"
    # baseline 1, pocket genus 0 → 0 < 1, fires.
    assert through_hole_check(req, pocket_stl, 1) == (1, 0)
    # baseline 1, through genus 1 → 1 does NOT exceed 1, fires.
    assert through_hole_check(req, through_stl, 1) == (1, 1)
    # baseline 0, through genus 1 → 1 exceeds 0, passes.
    assert through_hole_check(req, through_stl, 0) is None


def test_check_no_through_request_never_fires(tmp_path):
    """A blind/pocket/counterbore request (no ``through``) never
    triggers, even with a pocket mesh and a zero baseline."""
    stl = _write_stl(tmp_path / "pocket.stl", _pocket_stl())
    for req in (
        "drill a 5 mm hole 3 mm deep",
        "a counterbore 5 mm deep",
        "a pocket in the centre",
    ):
        assert through_hole_check(req, stl, 0) is None


def test_check_missing_stl_abstains(tmp_path):
    """An unreadable/missing STL path → ``None`` (abstain, one log
    line, the candidate proceeds)."""
    missing = str(tmp_path / "does-not-exist.stl")
    assert through_hole_check("drill a 6 mm hole through the box", missing, 0) is None
    # A file that exists but is not a mesh: load failure → abstain.
    bad = tmp_path / "garbage.stl"
    bad.write_bytes(b"this is not an stl file at all")
    assert through_hole_check("drill a 6 mm hole through the box", str(bad), 0) is None


def test_check_none_stl_abstains():
    """A render with no STL path → ``None`` (abstain)."""
    assert through_hole_check("drill a 6 mm hole through the box", None, 0) is None


def test_check_none_baseline_abstains(tmp_path):
    """An unknown baseline (``None`` — a corrupt/missing hole count) →
    ``None`` (abstain, never a fabricated baseline)."""
    stl = _write_stl(tmp_path / "pocket.stl", _pocket_stl())
    assert through_hole_check("drill a 6 mm hole through the box", stl, None) is None


def test_check_zero_watertight_components_abstains(tmp_path):
    """A render whose split yields zero watertight components → ``None``
    (abstain — an open, gapped mesh cannot speak to closed holes)."""
    bad = trimesh.Trimesh(
        vertices=[[0, 0, 0], [1, 0, 0], [0, 1, 0], [0, 0, 1]],
        faces=[[0, 1, 2], [0, 1, 3]],  # two triangles sharing an edge — OPEN
    )
    stl = _write_stl(tmp_path / "open.stl", bad)
    assert through_hole_check("drill a 6 mm hole through the box", stl, 0) is None


# ---------------------------------------------------------------------------
# genus_from_stl — the single public genus measurement
# ---------------------------------------------------------------------------


def test_genus_from_stl_box_is_zero():
    """A watertight box (genus 0) measures 0."""
    from d33d.part_mesh_topology import genus_from_stl

    assert genus_from_stl(str(_FIXTURE_DIR / "box_20mm.stl")) == 0


def test_genus_from_stl_through_hole_is_one():
    """The one-through-hole fixture measures 1."""
    from d33d.part_mesh_topology import genus_from_stl

    assert genus_from_stl(str(_FIXTURE_DIR / "through_hole_genus1.stl")) == 1


def test_genus_from_stl_three_hole_plate_is_three():
    """The three-through-hole plate fixture measures 3."""
    from d33d.part_mesh_topology import genus_from_stl

    assert genus_from_stl(str(_FIXTURE_DIR / "through_hole_genus3.stl")) == 3


def test_genus_from_stl_missing_path_is_none(tmp_path):
    """A missing path → ``None`` (abstain, no exception)."""
    from d33d.part_mesh_topology import genus_from_stl

    assert genus_from_stl(str(tmp_path / "does-not-exist.stl")) is None


def test_genus_from_stl_garbage_bytes_is_none(tmp_path):
    """Garbage bytes → ``None`` (load failure logged, abstain)."""
    from d33d.part_mesh_topology import genus_from_stl

    bad = tmp_path / "garbage.stl"
    bad.write_bytes(b"this is not an stl file at all")
    assert genus_from_stl(str(bad)) is None


# ---------------------------------------------------------------------------
# The check contract (baseline comparison, abstain cases)
# ---------------------------------------------------------------------------


def test_check_unexpected_exception_abstains(tmp_path, monkeypatch):
    """Issue #386 (adversarial round 1): an UNEXPECTED exception from
    ``mesh_topology`` (e.g. ``RuntimeError``) must make the check ABSTAIN
    (log and return ``None``), never crash the design loop. The
    ``except Exception`` in ``_rendered_genus`` catches any exception
    type, not just ``(OSError, ValueError, RuntimeError)``."""
    stl = _write_stl(tmp_path / "pocket.stl", _pocket_stl())

    def _raise_runtime_error(*args, **kwargs):
        raise RuntimeError("simulated unexpected mesh error")

    import d33d.part_mesh_topology as pmt
    monkeypatch.setattr(pmt, "mesh_topology", _raise_runtime_error)
    # The check must abstain (None), not raise.
    assert through_hole_check("drill a 6 mm hole through the box", stl, 0) is None


# ---------------------------------------------------------------------------
# Issue #418: the article rule (new hole must raise, existing hole must
# not lower) + the stated-count requirement + per-run baseline logging
# ---------------------------------------------------------------------------


def test_article_existing_hole_resize_unchanged_genus_passes(tmp_path):
    """Issue #418 (the v108 repro): a request about an EXISTING hole
    (definite article — "make the through-hole 8 mm") with parent genus
    1 and a rendered genus 1 must PASS (the old strict-exceed rule
    failed it as (1, 1))."""
    stl = _write_stl(tmp_path / "through.stl", _through_stl())
    assert through_hole_check("make the through-hole 8 mm", stl, 1) is None
    # A move of the existing hole — unchanged genus also passes.
    assert through_hole_check("move the through-hole to the left", stl, 1) is None


def test_article_existing_hole_fell_below_baseline_fails(tmp_path):
    """Issue #418: an existing-hole request where the rendered genus
    FELL below the parent baseline (a hole that closed) fails."""
    stl = _write_stl(tmp_path / "pocket.stl", _pocket_stl())
    # Parent genus 1, rendered genus 0 → the hole closed → (1, 0).
    assert through_hole_check("make the through-hole 8 mm", stl, 1) == (1, 0)


def test_article_new_hole_unchanged_genus_fails(tmp_path):
    """Issue #418: a NEW-hole request (indefinite article — "drill a
    hole through") over a part that already has holes (parent genus 4,
    rendered genus 4) must FAIL — the genus did not rise. A rendered
    genus that exceeds the baseline passes."""
    stl4 = _write_stl(tmp_path / "genus4.stl", _genus_n_stl(4))
    req = "drill a hole through the plate"
    # Parent genus 4, rendered genus 4 → no rise → (4, 4).
    assert through_hole_check(req, stl4, 4) == (4, 4)
    # Baseline 3, rendered genus 4 → one new hole → pass.
    assert through_hole_check(req, stl4, 3) is None


def test_article_new_hole_stated_count_requires_rise_by_count(tmp_path):
    """Issue #418: a stated count ("add two holes through") requires the
    genus to rise by the count: parent genus 2, rendered genus 3 → FAIL
    (only one new hole), rendered genus 4 → PASS (two new holes)."""
    stl3 = _write_stl(tmp_path / "genus3.stl", _genus_n_stl(3))
    stl4 = _write_stl(tmp_path / "genus4.stl", _genus_n_stl(4))
    req = "add two holes through the plate"
    assert through_hole_check(req, stl3, 2) == (2, 3)
    assert through_hole_check(req, stl4, 2) is None


def test_article_hyphenated_existing_hole_passes_unchanged(tmp_path):
    """Issue #418: the hyphenated token "through-hole" with a definite
    article is an existing-hole request — unchanged genus passes."""
    stl = _write_stl(tmp_path / "through.stl", _through_stl())
    assert through_hole_check("make the through-hole 8 mm", stl, 1) is None


def test_requested_hole_count_parses_counts():
    """Issue #418: the stated-count parser — a number word or an Arabic
    numeral is the count; no count → 1."""
    assert requested_hole_count("add two holes through") == 2
    assert requested_hole_count("drill 3 holes through the plate") == 3
    assert requested_hole_count("four through-holes in a row") == 4
    assert requested_hole_count("drill a hole through") == 1
    assert requested_hole_count("make the through-hole 8 mm") == 1


def test_check_logs_baseline_and_source(tmp_path, caplog):
    """Issue #418 (acceptance): each run logs the baseline used and the
    check's decision, so QA can see why the check passed or failed.
    The ``baseline_source`` kwarg (the adapter seam's provenance string)
    is logged WITH the decision — not just the baseline number."""
    stl = _write_stl(tmp_path / "through.stl", _through_stl())
    import logging

    src = "parent version v107 rendered genus: 1"
    with caplog.at_level(logging.INFO, logger="d33d.through_hole_check"):
        through_hole_check("make the through-hole 8 mm", stl, 1, src)
    assert any("existing hole" in rec.message for rec in caplog.records)
    assert any("baseline genus 1" in rec.message for rec in caplog.records)
    assert any(src in rec.message for rec in caplog.records), (
        "the baseline source must be logged with the decision"
    )

    src2 = "stored repaired part genus: 0"
    with caplog.at_level(logging.INFO, logger="d33d.through_hole_check"):
        through_hole_check("drill a hole through the plate", stl, 0, src2)
    assert any("new hole" in rec.message for rec in caplog.records)
    assert any(src2 in rec.message for rec in caplog.records), (
        "the baseline source must be logged with the decision (new-hole)"
    )

    # No source (the seam omitted the kwarg): the log says so honestly.
    with caplog.at_level(logging.INFO, logger="d33d.through_hole_check"):
        through_hole_check("make the through-hole 8 mm", stl, 1)
    assert any(
        "not set by the seam" in rec.message for rec in caplog.records
    )


def test_route_through_hole_repair_accepts_source_kwarg(tmp_path):
    """Issue #418: ``route_through_hole_repair`` accepts the
    ``through_baseline_genus_source`` kwarg (the adapter seam's
    provenance string) and forwards it to the check. Without the
    forward, the production path would raise ``TypeError``."""
    from d33d.through_hole_check import route_through_hole_repair

    stl = _write_stl(tmp_path / "pocket.stl", _pocket_stl())
    src = "parent version v108 rendered genus: 1"
    # An existing-hole request where the genus fell below the baseline:
    # the check fires (returns a tuple, not None) — the source kwarg
    # must not cause a TypeError.
    result = route_through_hole_repair(
        "make the through-hole 8 mm",
        stl,
        1,
        "W=20; cube([W,W,W]); difference();",
        src,
    )
    assert result is not None
    evidence, _instruction = result
    assert "fell below" in evidence
