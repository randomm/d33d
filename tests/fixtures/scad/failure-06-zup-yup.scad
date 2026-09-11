// Failure class 6: Z-up vs Y-up confusion
// The model is designed in Y-up convention (Blender) but OpenSCAD uses
// Z-up. The vertical movement is applied in Y instead of Z, producing
// a model that is flat or incorrectly oriented.
translate([0, 50, 0])
cube([20, 5, 20]);
