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

import trimesh

from d33d.through_hole_check import is_through_request, through_hole_check

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


def _box_minus_cylinder(blind: bool) -> trimesh.Trimesh:
    """A 20 × 20 × 20 box with a 6 mm-diameter cylindrical cut at the
    centre: ``blind`` — the cylinder goes 10 mm into the box (a pocket,
    genus 0); ``not blind`` — the cylinder pierces both faces (a
    through-hole, genus 1)."""
    box = trimesh.creation.box(extents=(20, 20, 20))
    height = 12 if blind else 44
    z0 = 0 if blind else -12
    cyl = trimesh.creation.cylinder(radius=3.0, height=height)
    cyl.apply_translation([0, 0, z0])
    out = box.difference(cyl)
    if isinstance(out, trimesh.Scene):
        out = out.to_mesh()
    return out


def _write_stl(path, mesh: trimesh.Trimesh) -> str:
    mesh.export(str(path))
    return str(path)


def test_pocket_mesh_has_genus_zero_and_through_mesh_genus_one():
    """Sanity: the test meshes themselves carry the expected topology —
    a blind pocket (euler 2 → genus 0), a through-hole (euler 0 → genus
    1)."""
    pocket = _box_minus_cylinder(blind=True)
    through = _box_minus_cylinder(blind=False)
    from d33d.part_holes import watertight_genus

    assert watertight_genus(pocket.split(only_watertight=False)) == 0
    assert watertight_genus(through.split(only_watertight=False)) == 1


def test_check_pocket_over_zero_baseline_fires(tmp_path):
    """A through request + a pocket mesh (genus 0) over a zero baseline
    (a new design) → the DETECTION tuple (0, 0) — the candidate fails,
    routed geometrically_wrong by the loop."""
    stl = _write_stl(tmp_path / "pocket.stl", _box_minus_cylinder(True))
    det = through_hole_check("drill a 6 mm hole through the box", stl, 0)
    assert det == (0, 0)


def test_check_through_mesh_over_zero_baseline_passes(tmp_path):
    """A through request + a true through-hole (genus 1) over a zero
    baseline → ``None`` (the check passes — the hole did pass)."""
    stl = _write_stl(tmp_path / "through.stl", _box_minus_cylinder(False))
    assert through_hole_check("drill a 6 mm hole through the box", stl, 0) is None


def test_check_through_request_over_part_baseline(tmp_path):
    """The baseline comparison: a part whose stored hole count is 1
    (baseline 1) — a pocket render (genus 0) does NOT exceed it (fires),
    a through render (genus 1... does NOT exceed 1 — a single hole on a
    one-hole part; the operator rule is EXCEED, so it also fires), and
    a part baseline the render EXCEEDS passes."""
    pocket_stl = _write_stl(tmp_path / "pocket.stl", _box_minus_cylinder(True))
    through_stl = _write_stl(tmp_path / "through.stl", _box_minus_cylinder(False))
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
    stl = _write_stl(tmp_path / "pocket.stl", _box_minus_cylinder(True))
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
    stl = _write_stl(tmp_path / "pocket.stl", _box_minus_cylinder(True))
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
