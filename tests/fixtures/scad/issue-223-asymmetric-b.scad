// Asymmetric-bbox regression fixture (issue #223, golden-b).
//
// Reconstructed from the real v16 model (project 32): the harvested STL's
// bounding box is x[0, 40.09], y[-18.15, 20], z[0, 30] — the silhouette is
// NOT symmetric about the origin, the x range is entirely positive, and the
// y range is skewed negative. The front view (camera -Z) clips the right
// frame edge and the top view (camera +Z) clips the right and top edges
// unless the per-model fit is correct.
//
// Polyhedron box with a central bore; the bore leaves the outer hull
// exactly the 40.09 × 38.15 × 30 box above (the bore only removes
// interior material, the hull is unchanged).
difference() {
  polyhedron(
    points = [
      [0, -18.15, 0], [40.09, -18.15, 0], [40.09, 20, 0], [0, 20, 0],
      [0, -18.15, 30], [40.09, -18.15, 30], [40.09, 20, 30], [0, 20, 30]
    ],
    faces = [
      [0, 1, 2, 3], [4, 7, 6, 5], [0, 4, 5, 1], [1, 5, 6, 2], [2, 6, 7, 3], [3, 7, 4, 0]
    ]
  );
  translate([20.05, 0, 0])
    cylinder(h=31, d=35);
}
