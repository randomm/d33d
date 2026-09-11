// Failure class 11: compiles cleanly but is geometrically wrong
// This fixture is syntactically valid OpenSCAD and will compile and
// render successfully. However, the geometry does not match the
// reference photo or the stated dimensions. Only the vision loop
// (bounding-box gate + vision critique) can catch this class.
// The shape is a valid cube but with wrong proportions.
cube([20, 20, 20]);
