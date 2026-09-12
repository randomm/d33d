// Qwen smoke baseline: a 25×30×50 mm block with a 12 mm through-hole.
// The operator has confirmed the model works in practice — this case is
// the reference every future model is measured against (not a gate).
W = 25;
D = 30;
H = 50;
HOLE_D = 12;

module block() {
  cube([W, D, H]);
}

module hole() {
  translate([W / 2, D / 2, 0])
  cylinder(h=H, d=HOLE_D);
}

difference() {
  block();
  hole();
}
