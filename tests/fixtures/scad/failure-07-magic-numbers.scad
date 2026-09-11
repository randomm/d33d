// Failure class 7: magic numbers instead of parameters
// All dimensions are inlined as numeric literals. The user's stated
// dimensions should appear as named parameters, never as inline values.
cube([20, 25, 30]);
cylinder(h=30, d=20);
sphere(r=10);
