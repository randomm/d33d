// Failure class 2: transform composition order
// The order of transforms matters: rotate then translate vs translate then rotate
// produce different results. This fixture applies transforms in the wrong
// order, producing a shape at an unexpected location.
translate([50, 0, 0])
rotate([0, 0, 90])
cube([10, 10, 10]);
