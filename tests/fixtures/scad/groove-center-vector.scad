// Groove cut with a wrong-typed center vector (issue #436).
// OpenSCAD takes center as a boolean; the vector is silently ignored.
difference() {
  cube(size = [40, 20, 10], center = [true, true, true]);
  translate([0, 0, 5]) cylinder(h = 12, r = 3, center = true);
}
