// Baseline for the boolean/topology CGAL cases: a tube (cylinder with a
// coaxial hole) 30 mm OD, 12 mm ID, 40 mm tall.
OD = 30;
ID = 12;
HGT = 40;

module outer_cyl() {
  cylinder(h=HGT, d=OD);
}

module inner_hole() {
  cylinder(h=HGT, d=ID);
}

difference() {
  outer_cyl();
  inner_hole();
}
