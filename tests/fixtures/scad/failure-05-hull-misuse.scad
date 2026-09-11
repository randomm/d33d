// Failure class 5: hull() misuse
// hull() requires at least two children to compute the convex hull.
// With only one child, the result is just that child (no hull computed),
// which is likely not the intended behavior.
hull() {
  cube([10, 10, 10]);
}
