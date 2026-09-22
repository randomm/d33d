// Asymmetric-bbox regression fixture (issue #223, golden-a).
//
// Reconstructed from the real v15 model (project 32): the harvested STL's
// bounding box is x[-22.5, 45], y[0, 20.1], z[0, 30] — the silhouette is
// NOT symmetric about the origin, and the bbox does not touch the origin
// on the negative-x or +y sides. The front view (camera -Z) clips the
// right frame edge unless the per-model fit is correct.
//
// Polyhedron box with two M4-style through-holes; the holes leave the
// outer hull exactly the 67.5 × 20.1 × 30 box above (holes only remove
// interior material, the hull is unchanged).
difference() {
  polyhedron(
    points = [
      [-22.5, 0, 0], [45, 0, 0], [45, 20.1, 0], [-22.5, 20.1, 0],
      [-22.5, 0, 30], [45, 0, 30], [45, 20.1, 30], [-22.5, 20.1, 30]
    ],
    faces = [
      [0, 1, 2, 3], [4, 7, 6, 5], [0, 4, 5, 1], [1, 5, 6, 2], [2, 6, 7, 3], [3, 7, 4, 0]
    ]
  );
  translate([11.25, 0, 0])
    cylinder(h=31, d=3.6);
}
