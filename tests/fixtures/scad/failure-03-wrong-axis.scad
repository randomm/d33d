// Failure class 3: rotation about the wrong axis
// The intent is to rotate 90 degrees about the Z-axis (spinning the shape
// in the XY plane), but the code rotates about the Y-axis instead, tilting
// the shape into the XZ plane.
rotate([0, 90, 0])
cube([10, 10, 10]);
