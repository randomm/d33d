"""Issue #419: generate the committed small-hole-moved fixture pair ONCE.

Runs the Blender EXACT boolean solver (the only reliable backend in the
project venv) in a single process so the two STLs share one triangulation
context, exactly like ``gen_v100_fixtures.py``. The pair proves the
fingerprint-based unchanged-mesh check with NO trimesh boolean at test
time:

  v100-plate-small-hole-A.stl  – 120×80×6 plate, a Ø3 mm bore at (0, 0)
  v100-plate-small-hole-B.stl  – same plate, the same bore at (10, 0)

The move is the PM's failure case for the summary-stats check: a small
hole (Ø3, 7.07 mm3) moved 10 mm on a large plate (~57.5 k mm3) shifts the
volume centroid by ~0.007 mm — far inside the old centroid tolerance —
and the volume stays identical (delta < 1e-6 mm3); even the face count
differs by only ~1% (the export triangulates the bore wall slightly
differently at the two positions). A fingerprint (the rounded exact
vertex set, sha256-hashed) separates the two files even though every
summary stat says "unchanged".

The pair is NOT a pair of identical vertex sets (a moved hole moves
vertices), so it also doubles as the proof that a genuine geometric
change, however small, is never "unchanged" under the fingerprint check
— while the stats path's tolerances would have swallowed it.

Both meshes are watertight and single-component, so the path-based check
load (``trimesh.load(process=False, force="mesh")``) and the seam load
(``load_and_split`` → watertight components) measure identical vertex and
face sets for each file.
"""

from __future__ import annotations

from pathlib import Path

import trimesh
from trimesh.creation import box, cylinder

FIXTURES_DIR = Path(__file__).parent.parent / "tests" / "fixtures" / "stl"

PLATE = (120.0, 80.0, 6.0)
BORE_RADIUS_MM = 1.5  # Ø3 mm hole
BORE_HEIGHT_MM = 60.0  # well beyond the 6 mm plate — clean through-bore
BORE_SECTIONS = 100
HOLE_POSITIONS_MM = (
    (0.0, 0.0),
    (10.0, 0.0),
)


def _plate_with_small_bore(cx: float, cy: float) -> "trimesh.Trimesh":
    p = box(PLATE, center=True)
    h = cylinder(
        radius=BORE_RADIUS_MM,
        height=BORE_HEIGHT_MM,
        sections=BORE_SECTIONS,
    )
    h.apply_translation([cx, cy, 0.0])
    return trimesh.boolean.difference([p, h])


def main() -> None:
    FIXTURES_DIR.mkdir(parents=True, exist_ok=True)
    meshes = {
        "v100-plate-small-hole-A.stl": _plate_with_small_bore(*HOLE_POSITIONS_MM[0]),
        "v100-plate-small-hole-B.stl": _plate_with_small_bore(*HOLE_POSITIONS_MM[1]),
    }
    for name, mesh in meshes.items():
        path = FIXTURES_DIR / name
        mesh.export(str(path))
        vol = float(mesh.volume)
        print(
            f"{name}: faces={len(mesh.faces)} vol={vol:.4f} mm3 "
            f"cm={[round(float(x), 6) for x in mesh.center_mass]} "
            f"bounds={mesh.bounds.tolist()}"
        )
    a, b = meshes.values()
    da = abs(float(a.volume) - float(b.volume))
    cm_shift = [
        round(float(x) - float(y), 9)
        for x, y in zip(a.center_mass, b.center_mass)
    ]
    print(
        f"centroid shift: {cm_shift} mm "
        f"(volume delta {da:.6e} mm3, "
        f"face-count delta {abs(len(a.faces) - len(b.faces))})"
    )


if __name__ == "__main__":
    main()
