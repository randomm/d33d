// Failure class 1: trailing semicolon after translate modifier
// The semicolon terminates the modifier, leaving the cube at the origin
// instead of being translated.
translate([10, 20, 30]);
cube([10, 10, 10]);
