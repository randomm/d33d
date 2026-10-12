// Lid, correct (issue #436): the plate and the rim strips are unioned, so the
// render reaches the declared 3.5 mm stack.
lid_base_thickness = 1.5;
rim_drop = 1.0;
skirt_height = 1.0;
lid_base = lid_base_thickness + rim_drop;
total_height = lid_base + skirt_height;

union() {
  cube([55, 40, lid_base]);
  translate([3, 3, lid_base]) cube([49, 34, skirt_height]);
}
