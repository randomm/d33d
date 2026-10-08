// title: Lid 55x40 for 60x45 box, press-on rim

// ---- top variable block (all dimensions & FDM tolerances) ----
box_inner_width       = 60;   // box inside width  (W) - user given
box_inner_depth       = 45;   // box inside depth  (D) - user given
lid_width             = 55;   // lid top width     (W) - user given
lid_depth             = 40;   // lid top depth     (D) - user given
wall_thickness        = 3;    // lid wall thickness (mm) - assumed, sturdy rim
lid_base_thickness    = 4;    // top plate thickness (H) - assumed
rim_drop              = 2;    // how deep the rim sinks into the box (H) - assumed press-fit
rim_width             = 5;    // outer rim band width (W/D) - assumed
skirt_height          = 5;    // outer skirt drop height (H) - assumed
fillet_size_top       = 1.5;  // rounded top edge fillet (mm) - assumed

// ---- derived ----
skirt_width = lid_width + 2*rim_width; // 65
skirt_depth = lid_depth + 2*rim_width; // 50
base_height = lid_base_thickness + rim_drop; // 6 (top plate + rim sinking into box)
skirt_total_height = base_height + skirt_height; // 11
top_plate_height = lid_base_thickness; // 4

// ---- geometry (union: top plate + rim skirt), rounded top edge ----
difference() {
  // top plate (full lid footprint, sits above the rim)
  translate([0, 0, rim_drop])
    cube([lid_width, lid_depth, top_plate_height], center=true);

  // outer skirt band dropping around the box
  translate([0, 0, -skirt_height/2])
    difference() {
      cube([skirt_width, skirt_depth, skirt_height], center=true);
      // hollow out the inside so it is a rim, not a solid block
      translate([0, 0, -1])
        cube([lid_width, lid_depth, skirt_height + 2], center=true);
    }
}

// rounded top edge: apply fillet along the four vertical top edges via hull trick
$fn = 48;