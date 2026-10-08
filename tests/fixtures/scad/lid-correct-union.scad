// title: Lid for 60x45 box — CORRECT (renders as 60x45x11)
// Realistic correct-lid fixture, round 3: the height is the SUM of three
// features declared as derived params (plate + rim + skirt = 11), exactly
// the way the model writes a lid — a sum is present so the (S) gate has a
// declared stack to compare against the measured Z.

plate_thickness = 4;  // lid plate
rim_drop = 2;         // rim press depth
skirt_height = 5;     // outer skirt
box_inner_width = 60; // box inside width
box_inner_depth = 45; // box inside depth
lid_width = box_inner_width;   // lid covers the box's full inside width
lid_depth = box_inner_depth;   // ... and depth
base_height = plate_thickness + rim_drop;          // 6
total_height = base_height + skirt_height;         // 11 (declared stack)

// Plate: 60 x 45 x 4 at the bottom.
translate([0, 0, 0])
    cube([lid_width, lid_depth, plate_thickness]);

// Rim drop: a 58 x 43 x 2 centred pad on top of the plate.
translate([1, 1, plate_thickness])
    cube([lid_width - 2, lid_depth - 2, rim_drop]);

// Skirt: a 56 x 41 x 5 centred column on top of the rim.
translate([2, 2, plate_thickness + rim_drop])
    cube([lid_width - 4, lid_depth - 4, skirt_height]);
