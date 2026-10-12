// Groove cut with a boolean center (issue #436 correct form).
difference() {
  cube(size = [40, 20, 10], center = true);
  translate([0, 0, 5]) cylinder(h = 12, r = 3, center = true);
}
