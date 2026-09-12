// Baseline for red-region edit cases: a 20×20×20 mm box.
// The edit is module-scoped — everything not marked in red is preserved.
W = 20;
H = 20;
D = 20;

module base_box() {
  cube([W, H, D]);
}

base_box();
