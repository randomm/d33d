// v111 lid, broken (issue #436): the declared stack is 3.5 mm, but the rim
// strips were discarded by difference(), so only the 2.5 mm plate renders.
lid_base_thickness = 1.5;
rim_drop = 1.0;
skirt_height = 1.0;
lid_base = lid_base_thickness + rim_drop;
total_height = lid_base + skirt_height;

difference() {
  cube([55, 40, lid_base]);
  translate([0, 0, lid_base]) cube([55, 40, skirt_height]);
}
